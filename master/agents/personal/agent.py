"""
master.agents.personal.agent
==============================
PersonalAgent: daily assistant for scheduling, communication, and personal tasks.
Handles intents: schedule_meeting, set_reminder, compose_message,
                  summarise_calendar, daily_briefing, find_contact.

Tools: Gmail MCP, Google Calendar MCP, Notion MCP, Slack MCP (stub).
Context: Person + Task + Event + News nodes from Librarian (ACL-filtered).
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

_SYSTEM_PROMPT = """You are Lucifer's personal assistant agent.
You have access to the user's calendar, emails, and notes.
Always be concise, actionable, and privacy-conscious.
Classify data before including it in responses:
- Public/Standard: safe to summarise
- Restricted: acknowledge existence, do not quote content
- Secret: do not reference at all

Respond in markdown. For actions (send email, create event), output structured JSON
in a ```action``` block that will be executed by the MCP layer.
"""


class PersonalAgent(BaseAgent):
    """
    Personal assistant agent handling scheduling, communication, and task management.
    Uses Gmail, Calendar, Notion MCPs for read/write actions.
    High-risk actions (send email, delete event) automatically escalate for HitL.
    """

    AGENT_ID = "personal-agent"
    _HIGH_RISK_INTENTS = frozenset({"send_email", "delete_event", "compose_message"})

    async def execute(self, request: AgentRequest) -> AgentResponse:
        with tracer.start_as_current_span(f"agent.{self.AGENT_ID}.execute"):
            self._emit("agent.start", request, intent=request.intent)
            start = time.monotonic()

            # Escalate high-risk intents before any action
            if request.intent in self._HIGH_RISK_INTENTS and request.risk_tier == RiskTier.HIGH:
                return self._escalate_response(
                    request,
                    f"Intent '{request.intent}' requires user approval before execution.",
                )

            try:
                response_text, memory_deltas = await self._handle_intent(request)
            except Exception as exc:
                return self._error_response(request, str(exc))

            latency_ms = int((time.monotonic() - start) * 1000)
            self._emit(
                "agent.complete",
                request,
                intent=request.intent,
                latency_ms=str(latency_ms),
            )

            return AgentResponse(
                task_id=request.task_id,
                agent_id=self.AGENT_ID,
                status="success",
                result={"content": response_text},
                memory_deltas=memory_deltas,
                token_usage=TokenUsage(input_tokens=0, output_tokens=0),  # set by LLM call
                cost_usd=0.0,
            )

    async def _handle_intent(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """Dispatch to the appropriate handler based on intent."""
        handlers: dict[str, Any] = {
            "schedule_meeting": self._schedule_meeting,
            "summarise_calendar": self._summarise_calendar,
            "daily_briefing": self._daily_briefing,
            "chat": self._chat,
        }
        handler = handlers.get(request.intent, self._chat)
        return await handler(request)  # type: ignore[no-any-return]

    async def _chat(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """General chat: build context-aware prompt, call LLM, return response."""
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Context:\n{context_summary}\n\nUser: {request.raw_input}",
            ),
        ]

        provider = await self._llm.select(
            query=request.raw_input,
            agent_id=self.AGENT_ID,
            max_budget_usd=request.token_budget.max_cost_usd,
        )
        completion_req = CompletionRequest(
            messages=messages,
            model=provider.provider_id.split("-", 1)[-1],  # strip prefix
            max_tokens=request.token_budget.output_limit,
            temperature=0.7,
        )
        response = await self._llm.complete_with_retry(provider, completion_req)
        return response.content, []

    async def _summarise_calendar(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """Read calendar events and produce a structured summary."""
        if self._mcp is None:
            return "Calendar summary: (MCP client not available)", []
        result = await self._mcp.invoke("gcal", "list_events", {"max_results": 10})
        if not result.success:
            return f"Calendar summary: failed to fetch events ({result.error})", []
        events: list[dict[str, Any]] = result.output or []
        if not events:
            return "No upcoming events found.", []
        lines = [
            f"- **{e.get('summary', '(no title)')}** — {e.get('start', {}).get('dateTime', 'TBD')}"
            for e in events
        ]
        return "## Calendar\n\n" + "\n".join(lines), []

    async def _schedule_meeting(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """Find a free slot and create a calendar event."""
        if self._mcp is None:
            return "Scheduling: (MCP client not available)", []
        slot_result = await self._mcp.invoke("gcal", "find_slot", {"duration_minutes": 30})
        if not slot_result.success or not slot_result.output:
            return f"Could not find a free slot: {slot_result.error}", []
        slot: dict[str, Any] = slot_result.output
        create_result = await self._mcp.invoke(
            "gcal",
            "create_event",
            {
                "summary": request.raw_input,
                "start": slot.get("start"),
                "end": slot.get("end"),
            },
        )
        if not create_result.success:
            return f"Failed to create event: {create_result.error}", []
        return f"Meeting scheduled: {create_result.output}", []

    async def _daily_briefing(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """Compile the daily briefing: top news + calendar + pending tasks."""
        briefing = "# Daily Briefing\n\n"
        briefing += "📅 **Calendar**: (fetching via GCal MCP — Phase 3)\n\n"
        briefing += "📰 **News**: (fetching via News Engine — Phase 3)\n\n"
        briefing += "✅ **Tasks**: (fetching from Librarian — Phase 3)\n"
        return briefing, []
