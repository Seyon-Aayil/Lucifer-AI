"""
master.agents.librarian.memory_writer
======================================
Applies MemoryDelta batches to all three memory stores: Neo4j, Mem0, and Zep.

Routing by data classification:
  public / standard  → Neo4j + Mem0 + Zep
  restricted         → Neo4j + Zep only  (never cloud Mem0)
  secret             → not stored here; caller must never produce these deltas

Each store write is fire-and-forget: a failure in one store is logged but
does not prevent writes to the other stores. This keeps memory writes
non-blocking on the critical response path.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from master.agents.base.agent import MemoryDelta
from master.agents.librarian.mem0_client import Mem0Client
from master.agents.librarian.zep_client import ZepClient
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

if TYPE_CHECKING:
    from master.agents.librarian.graph_client import GraphClient

log = get_logger(__name__)
tracer = get_tracer(__name__)

_DEFAULT_USER_ID = "lucifer-user"  # single-user; scoped by session in Phase 4+

# Data classifications that are allowed in cloud/external stores
_CLOUD_SAFE = frozenset({"public", "standard"})


class MemoryWriter:
    """
    Applies batches of MemoryDeltas to Neo4j, Mem0, and Zep.
    Instantiated with a shared GraphClient from the orchestrator.
    """

    def __init__(self, graph_client: GraphClient | None = None) -> None:
        self._gc = graph_client
        self._mem0 = Mem0Client()
        self._zep = ZepClient()

    async def apply_deltas(
        self,
        deltas: list[MemoryDelta],
        agent_id: str = "",
        user_id: str = _DEFAULT_USER_ID,
    ) -> None:
        """
        Fan out each delta to the appropriate stores based on its classification.
        All writes happen concurrently; individual failures are logged silently.
        """
        if not deltas:
            return

        with tracer.start_as_current_span("memory_writer.apply_deltas"):
            log.info("memory_writer.apply", count=len(deltas), agent=agent_id)

            tasks: list[Any] = []
            mem0_messages: list[dict[str, str]] = []
            zep_messages: list[dict[str, str]] = []

            for delta in deltas:
                classification = delta.classification.lower()

                # ── Neo4j (all non-secret deltas) ────────────────────────────
                if classification != "secret" and self._gc is not None:
                    tasks.append(self._write_neo4j(delta))

                # ── Mem0 (cloud-safe only) ────────────────────────────────────
                if classification in _CLOUD_SAFE:
                    text = _delta_to_text(delta)
                    if text:
                        mem0_messages.append({"role": "assistant", "content": text})

                # ── Zep (non-secret) ─────────────────────────────────────────
                if classification != "secret":
                    text = _delta_to_text(delta)
                    if text:
                        zep_messages.append({"role": "ai", "content": text})

            # Batch Mem0 + Zep writes
            if mem0_messages:
                tasks.append(self._mem0.add(user_id=user_id, messages=mem0_messages))
            if zep_messages:
                tasks.append(self._zep.add_episode(user_id=user_id, messages=zep_messages))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                errors = [r for r in results if isinstance(r, Exception)]
                if errors:
                    log.warning("memory_writer.partial_failure", errors=[str(e) for e in errors])

    # ── Neo4j dispatch ────────────────────────────────────────────────────────

    async def _write_neo4j(self, delta: MemoryDelta) -> None:
        assert self._gc is not None
        try:
            if delta.operation == "upsert" and delta.node_type and delta.node_id:
                await self._gc.upsert_node(delta.node_type, delta.node_id, delta.attributes)
            elif delta.operation == "soft_delete" and delta.node_id:
                await self._gc.soft_delete_node(delta.node_id)
            elif (
                delta.operation == "edge_upsert"
                and delta.from_node_id
                and delta.to_node_id
                and delta.edge_relation
            ):
                await self._gc.upsert_edge(
                    delta.from_node_id, delta.to_node_id, delta.edge_relation
                )
        except Exception as exc:
            log.warning("memory_writer.neo4j_failed", op=delta.operation, error=str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _delta_to_text(delta: MemoryDelta) -> str:
    """
    Produce a plain-text representation of a delta for Mem0/Zep ingestion.
    Returns empty string for edge operations (no meaningful text to store).
    """
    if delta.operation in ("upsert",) and delta.node_type and delta.node_id:
        attrs = delta.attributes
        name = attrs.get("name") or attrs.get("title") or attrs.get("content") or ""
        if name:
            return f"{delta.node_type} '{name}' updated (id={delta.node_id})"
        return f"{delta.node_type} id={delta.node_id} updated"
    if delta.operation == "soft_delete" and delta.node_id:
        return f"Node {delta.node_id} deleted"
    return ""
