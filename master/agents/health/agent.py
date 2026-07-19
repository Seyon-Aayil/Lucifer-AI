"""
master.agents.health.agent
============================
HealthAgent: HealthKit data queries, medication reminders, and wellness summaries.

PRIVACY RULES (non-negotiable):
- All health data is classified RESTRICTED — it NEVER leaves the local device/process.
- The LLM provider selected MUST be a local model (Ollama). Cloud LLMs are blocked.
- No health data is written to external storage or sent over the network.
- Raw values (heart rate readings, exact weights, medication names) are never
  included verbatim in responses — only aggregate summaries are permitted.

Tools: None (HealthKit data accessed via local store only; no MCP).
Intents: health_query, medication_reminder, activity_summary, sleep_summary.
Risk: MEDIUM for all read intents.
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
)
from master.core.config import get_settings
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import CompletionRequest, Message, ProviderTier

log = get_logger(__name__)
tracer = get_tracer(__name__)

_SYSTEM_PROMPT = """You are Lucifer's health assistant agent running entirely on-device.
Health data is Restricted — provide aggregated insights only, never raw values.
Rules:
- Do not quote exact sensor readings, weights, or medication names.
- Do not suggest diagnoses or medical treatments.
- Always recommend consulting a healthcare professional for medical concerns.
- Keep responses brief and actionable.
"""


class HealthAgent(BaseAgent):
    """
    Health assistant. Uses local-only Ollama model — never sends data to cloud LLMs.
    All health data stays within the local process boundary.
    """

    AGENT_ID = "health-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:
        with tracer.start_as_current_span(f"agent.{self.AGENT_ID}.execute"):
            self._emit("agent.start", request, intent=request.intent)
            start = time.monotonic()

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
            "health_query": self._health_query,
            "medication_reminder": self._medication_reminder,
            "activity_summary": self._activity_summary,
            "sleep_summary": self._sleep_summary,
        }
        handler = handlers.get(request.intent, self._health_query)
        result = await handler(request)
        if isinstance(result, HandlerResult):
            return result
        text, deltas = result
        return HandlerResult(text=text, memory_deltas=deltas)

    async def _read_health_records(self, category: str) -> list[dict[str, Any]]:
        """
        Read HealthRecord nodes for a category from Neo4j.

        Records are synced from the edge HealthKit store (future: read the edge
        store directly; Neo4j HealthRecord nodes are the implemented path). The
        `category` is matched against the node's `category`/`type` attribute.
        """
        from master.agents.librarian.graph_client import GraphClient

        try:
            gc = GraphClient.from_settings()
            try:
                nodes = await gc.list_nodes_by_type("HealthRecord", limit=200, order_by="updatedAt")
            finally:
                await gc.close()
        except Exception as exc:
            log.warning("health_agent.records_read_failed", category=category, error=str(exc))
            return []
        return [n for n in nodes if (n.get("category") or n.get("type")) == category]

    @staticmethod
    def _aggregate(records: list[dict[str, Any]]) -> str:
        """Aggregate numeric `value` fields — never echo individual raw readings."""
        values = [
            float(n["value"])
            for n in records
            if isinstance(n.get("value"), (int, float))
            or (isinstance(n.get("value"), str) and n["value"].replace(".", "", 1).isdigit())
        ]
        if not values:
            return f"{len(records)} record(s) on file."
        avg = sum(values) / len(values)
        return f"{len(records)} record(s); aggregate average ≈ {round(avg, 1)}."

    async def _activity_summary(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        records = await self._read_health_records("activity")
        if not records:
            return "Activity summary: no recent activity data available.", []
        return f"Activity summary: {self._aggregate(records)}", []

    async def _sleep_summary(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        records = await self._read_health_records("sleep")
        if not records:
            return "Sleep summary: no recent sleep data available.", []
        return f"Sleep summary: {self._aggregate(records)}", []

    async def _medication_reminder(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        records = await self._read_health_records("medication")
        if not records:
            return "Medication reminders: no scheduled medications on file.", []
        # Privacy: never name the medications — report the count of scheduled items only.
        return f"Medication reminders: {len(records)} scheduled item(s) on your list.", []

    async def _health_query(self, request: AgentRequest) -> HandlerResult:
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Context:\n{context_summary}\n\nUser: {request.raw_input}",
            ),
        ]
        return await self._llm_complete_local(request, messages)

    async def _llm_complete_local(
        self, request: AgentRequest, messages: list[Message]
    ) -> HandlerResult:
        """Force local Ollama model — health data must never reach cloud APIs."""
        settings = get_settings()
        # Pin to the local (DESKTOP) tier so selection can only return a local
        # provider (Ollama) — health data must never reach a cloud API, and the
        # PII cloud-dispatch gate only guards MASTER-tier calls.
        selection = await self._llm.select(
            query=request.raw_input,
            tier=ProviderTier.DESKTOP,
            agent_id=self.AGENT_ID,
            max_budget_usd=request.token_budget.max_cost_usd,
        )
        provider = selection.provider
        log.info(
            "health_agent.llm_local",
            provider=provider.provider_id,
            ollama_url=settings.ollama_base_url,
        )
        # Local Ollama ignores the effort dial; pass it through for uniformity.
        completion_req = CompletionRequest(
            messages=messages,
            model=provider.provider_id.split("-", 1)[-1],
            max_tokens=request.token_budget.output_limit,
            effort=selection.effort,
        )
        return await self._run_llm(provider, completion_req)
