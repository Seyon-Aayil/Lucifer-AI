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
    MemoryDelta,
    RiskTier,
    TokenUsage,
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
                response_text, memory_deltas = await self._handle_intent(request)
            except Exception as exc:
                return self._error_response(request, str(exc))

            latency_ms = int((time.monotonic() - start) * 1000)
            self._emit("agent.complete", request, intent=request.intent, latency_ms=str(latency_ms))

            return AgentResponse(
                task_id=request.task_id,
                agent_id=self.AGENT_ID,
                status="success",
                result={"content": response_text},
                memory_deltas=memory_deltas,
                token_usage=TokenUsage(input_tokens=0, output_tokens=0),
                cost_usd=0.0,
            )

    async def _handle_intent(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        handlers: dict[str, Any] = {
            "check_budget":      self._check_budget,
            "spending_summary":  self._spending_summary,
            "financial_query":   self._financial_query,
        }
        handler = handlers.get(request.intent, self._financial_query)
        return await handler(request)

    async def _check_budget(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        # Phase 3: query SpendTracker / local financial store
        return (
            "Budget check: financial data store integration pending. "
            "Current daily LLM spend is tracked via SpendTracker.",
            [],
        )

    async def _spending_summary(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        # Phase 3: query local financial DB and summarise via LLM
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Context:\n{context_summary}\n\n"
                    f"User: {request.raw_input}\n\n"
                    "Note: Actual transaction data integration is pending Phase 3."
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _financial_query(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Context:\n{context_summary}\n\nUser: {request.raw_input}",
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _llm_complete(
        self, request: AgentRequest, messages: list[Message]
    ) -> tuple[str, list[MemoryDelta]]:
        provider = await self._llm.select(
            query=request.raw_input,
            agent_id=self.AGENT_ID,
            max_budget_usd=request.token_budget.max_cost_usd,
        )
        completion_req = CompletionRequest(
            messages=messages,
            model=provider.provider_id.split("-", 1)[-1],
            max_tokens=request.token_budget.output_limit,
            temperature=0.3,
        )
        response = await self._llm.complete_with_retry(provider, completion_req)
        return response.content, []
