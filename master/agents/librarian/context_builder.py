"""
master.agents.librarian.context_builder
=======================================
Builds ACL-filtered ContextPackages for requesting agents.

Pulls context from three stores in parallel:
  1. Neo4j (semantic graph) — keyword/full-text search over nodes
  2. Mem0  (episodic)       — semantic search over past interactions
  3. Zep   (temporal)       — temporal fact search over episode stream

Results are ACL-filtered per agent, then assembled into a ContextPackage
whose `summary` field contains a 200-token-budget plain-text digest.

All store failures degrade gracefully — a partial context package is
always returned so that agent execution is never blocked by memory outages.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from master.agents.base.agent import ContextPackage
from master.agents.librarian.access_control import filter_nodes_for_agent
from master.agents.librarian.mem0_client import Mem0Client
from master.agents.librarian.zep_client import ZepClient
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

if TYPE_CHECKING:
    from master.agents.librarian.graph_client import GraphClient

log = get_logger(__name__)
tracer = get_tracer(__name__)

# Token budget for the summary string (conservative estimate: 1 token ≈ 4 chars)
_SUMMARY_CHAR_BUDGET = 800
_DEFAULT_USER_ID = "lucifer-user"  # single-user; replaced by session user_id in Phase 4+


class ContextBuilder:
    """
    Assembles an ACL-filtered ContextPackage from Neo4j, Mem0, and Zep.
    Instantiated per-request by context_inject_node with a fresh GraphClient.
    """

    def __init__(self, graph_client: GraphClient | None = None) -> None:
        self._gc = graph_client
        self._mem0 = Mem0Client()
        self._zep = ZepClient()

    async def build(self, agent_id: str, intent: str) -> ContextPackage:
        """
        Build an ACL-filtered ContextPackage for the requesting agent.
        Queries all three memory stores in parallel; failures return empty results.
        """
        with tracer.start_as_current_span("context_builder.build"):
            query = intent  # intent string used as the search query seed

            # Fan out to all stores concurrently
            neo4j_nodes, mem0_memories, zep_facts = await asyncio.gather(
                self._fetch_neo4j(agent_id, query),
                self._fetch_mem0(query),
                self._fetch_zep(query),
                return_exceptions=False,
            )

            # ACL-filter graph nodes
            allowed_nodes = filter_nodes_for_agent(agent_id, neo4j_nodes, operation="read")

            summary = _build_summary(agent_id, intent, allowed_nodes, mem0_memories, zep_facts)
            token_estimate = len(summary) // 4  # rough estimate

            log.info(
                "context_builder.built",
                agent=agent_id,
                intent=intent,
                nodes=len(allowed_nodes),
                memories=len(mem0_memories),
                facts=len(zep_facts),
                chars=len(summary),
            )

            return ContextPackage(
                requesting_agent=agent_id,
                task_type=intent,
                nodes=allowed_nodes,
                edges=[],
                summary=summary,
                token_estimate=token_estimate,
            )

    # ── Store fetchers ────────────────────────────────────────────────────────

    async def _fetch_neo4j(self, agent_id: str, query: str) -> list[dict[str, Any]]:
        if self._gc is None:
            return []
        try:
            async with self._gc._driver.session() as session:
                result = await session.run(
                    """
                    MATCH (n)
                    WHERE n.deletedAt IS NULL
                      AND (n.name CONTAINS $q OR n.title CONTAINS $q OR n.content CONTAINS $q)
                    RETURN n
                    LIMIT 20
                    """,
                    q=query,
                )
                records = await result.data()
                return [{"labels": list(r["n"].labels), **dict(r["n"])} for r in records]
        except Exception as exc:
            log.warning("context_builder.neo4j_failed", error=str(exc))
            return []

    async def _fetch_mem0(self, query: str) -> list[dict[str, Any]]:
        return await self._mem0.search(user_id=_DEFAULT_USER_ID, query=query, limit=10)

    async def _fetch_zep(self, query: str) -> list[dict[str, Any]]:
        return await self._zep.search(user_id=_DEFAULT_USER_ID, query=query, limit=10)


# ── Summary builder ───────────────────────────────────────────────────────────


def _build_summary(
    agent_id: str,
    intent: str,
    nodes: list[dict[str, Any]],
    memories: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> str:
    """
    Assemble a plain-text summary from retrieved context, capped at the char budget.
    No LLM call — keeps context injection on the critical path fast.
    """
    parts: list[str] = [f"Agent: {agent_id} | Intent: {intent}"]

    if nodes:
        node_lines = [
            f"  - [{'/'.join(n.get('labels', ['?']))}] {n.get('name') or n.get('title') or n.get('id', '?')}"
            for n in nodes[:10]
        ]
        parts.append("Graph nodes:\n" + "\n".join(node_lines))

    if memories:
        mem_lines = [f"  - {m.get('memory', m.get('content', '?'))[:120]}" for m in memories[:5]]
        parts.append("Episodic memory:\n" + "\n".join(mem_lines))

    if facts:
        fact_lines = [
            f"  - {f.get('content', f.get('message', {}).get('content', '?'))[:120]}"
            for f in facts[:5]
        ]
        parts.append("Temporal facts:\n" + "\n".join(fact_lines))

    summary = "\n\n".join(parts)
    return summary[:_SUMMARY_CHAR_BUDGET]
