"""
master.agents.financial.agent
================================
FinancialAgent: spend tracking, budget alerts, and financial queries.

IMPORTANT: Every intent in this agent is HIGH risk.
The agent NEVER executes write operations autonomously — it always escalates
for human approval (HitL). Read-only queries (check_budget, spending_summary)
are permitted to complete, but any action affecting funds escalates.

Tools: None (financial data stays local; no external MCP for finances).
Intents: check_budget, spending_summary, set_alert, financial_query.
Risk: HIGH for all intents.
"""

from __future__ import annotations

import time
from typing import Any

from master.agents.base.agent import (
    AgentRequest,
    AgentResponse,
    BaseAgent,
    HandlerResult,
    MemoryDelta,
    RiskTier,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import CompletionRequest, Message

log = get_logger(__name__)
tracer = get_tracer(__name__)

_SYSTEM_PROMPT = """You are Lucifer's financial assistant agent.
You help the user understand their spending and budget.
STRICT RULES:
- Never suggest, initiate, or describe steps to transfer money or execute trades.
- Classify all financial data as Restricted — do not include raw account numbers or balances in responses.
- If asked to perform a transaction, always respond: "This action requires your direct approval."
- Provide summaries and insights only; let the user take any real-world action themselves.
"""

# All write intents must be escalated regardless of incoming risk_tier
_WRITE_INTENTS = frozenset({"set_alert", "update_budget", "pay_bill", "transfer"})

# Read intents allowed to complete autonomously (still logged + audited)
_READ_INTENTS = frozenset({"check_budget", "spending_summary", "financial_query"})


def _amount(node: dict[str, Any]) -> float:
    """Best-effort numeric amount for a Financial node (0.0 if missing/invalid)."""
    try:
        return float(node.get("amount", 0))
    except (TypeError, ValueError):
        return 0.0


class FinancialAgent(BaseAgent):
    """
    Financial assistant. Read queries complete autonomously; any write
    intent (including set_alert) always escalates for HitL.
    """

    AGENT_ID = "financial-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:
        with tracer.start_as_current_span(f"agent.{self.AGENT_ID}.execute"):
            self._emit("agent.start", request, intent=request.intent)
            start = time.monotonic()

            # All write intents escalate unconditionally
            if request.intent in _WRITE_INTENTS or request.risk_tier == RiskTier.CRITICAL:
                return self._escalate_response(
                    request,
                    f"Financial action '{request.intent}' requires your direct approval.",
                )

            try:
                result = await self._handle_intent(request)
            except Exception as exc:
                return self._error_response(request, str(exc))

            latency_ms = int((time.monotonic() - start) * 1000)
            self._emit("agent.complete", request, intent=request.intent, latency_ms=str(latency_ms))

            return AgentResponse(
                task_id=request.task_id,
                agent_id=self.AGENT_ID,
                status="success",
                result={"content": result.text},
                memory_deltas=result.memory_deltas,
                token_usage=result.token_usage,
                cost_usd=result.cost_usd,
            )

    async def _handle_intent(self, request: AgentRequest) -> HandlerResult:
        handlers: dict[str, Any] = {
            "check_budget": self._check_budget,
            "spending_summary": self._spending_summary,
            "financial_query": self._financial_query,
        }
        handler = handlers.get(request.intent, self._financial_query)
        result = await handler(request)
        if isinstance(result, HandlerResult):
            return result
        text, deltas = result
        return HandlerResult(text=text, memory_deltas=deltas)

    async def _read_financial_nodes(self) -> list[dict[str, Any]] | None:
        """Read Financial nodes from Neo4j; None on read failure (vs [] = empty)."""
        from master.agents.librarian.graph_client import GraphClient

        try:
            gc = GraphClient.from_settings()
            try:
                return await gc.list_nodes_by_type("Financial", limit=500, order_by="updatedAt")
            finally:
                await gc.close()
        except Exception as exc:
            log.warning("financial_agent.read_failed", error=str(exc))
            return None

    async def _check_budget(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """
        Read Financial nodes and report an aggregate budget-vs-spend status.

        Reports rounded totals only — never raw account numbers or per-account
        balances (see the agent's RESTRICTED-data rules above).
        """
        nodes = await self._read_financial_nodes()
        if nodes is None:
            return "Budget check: financial data is temporarily unavailable.", []
        if not nodes:
            return "Budget check: no budget data on file yet.", []

        budget = sum(_amount(n) for n in nodes if n.get("kind") == "budget")
        spend = sum(_amount(n) for n in nodes if n.get("kind") in ("spend", "transaction"))

        if budget <= 0:
            return (
                f"Budget check: {len(nodes)} financial record(s) on file; no budget target set.",
                [],
            )
        remaining = budget - spend
        status = "within budget" if remaining >= 0 else "over budget"
        return (
            f"Budget check: {status}. "
            f"Spent ≈ {round(spend, 2)} of {round(budget, 2)} "
            f"(≈ {round(remaining, 2)} remaining).",
            [],
        )

    async def _spending_summary(self, request: AgentRequest) -> HandlerResult:
        """Summarise spending from live Financial nodes (aggregated, no raw values)."""
        nodes = await self._read_financial_nodes()
        if not nodes:
            spend_block = "No transaction data on file."
        else:
            spend = sum(_amount(n) for n in nodes if n.get("kind") in ("spend", "transaction"))
            budget = sum(_amount(n) for n in nodes if n.get("kind") == "budget")
            txns = [n for n in nodes if n.get("kind") in ("spend", "transaction")]
            # Aggregate by category only — never expose raw account numbers.
            by_cat: dict[str, float] = {}
            for n in txns:
                by_cat[str(n.get("category", "uncategorised"))] = by_cat.get(
                    str(n.get("category", "uncategorised")), 0.0
                ) + _amount(n)
            cat_lines = "; ".join(f"{c}: ≈{round(v, 2)}" for c, v in sorted(by_cat.items()))
            spend_block = (
                f"{len(txns)} transaction(s); total spend ≈ {round(spend, 2)}"
                f"{f' of {round(budget, 2)} budget' if budget else ''}. "
                f"By category — {cat_lines or '(none)'}."
            )

        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Context:\n{context_summary}\n\n"
                    f"Spending data (aggregated):\n{spend_block}\n\n"
                    f"User: {request.raw_input}"
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _financial_query(self, request: AgentRequest) -> HandlerResult:
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Context:\n{context_summary}\n\nUser: {request.raw_input}",
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _llm_complete(self, request: AgentRequest, messages: list[Message]) -> HandlerResult:
        selection = await self._llm.select(
            query=request.raw_input,
            agent_id=self.AGENT_ID,
            max_budget_usd=request.token_budget.max_cost_usd,
        )
        provider = selection.provider
        completion_req = CompletionRequest(
            messages=messages,
            model=provider.provider_id.split("-", 1)[-1],
            max_tokens=request.token_budget.output_limit,
            effort=selection.effort,
        )
        return await self._run_llm(provider, completion_req)
