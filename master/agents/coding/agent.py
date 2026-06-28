"""
master.agents.coding.agent
============================
CodingAgent: code review, PR analysis, issue creation, and repo summaries.

Tools: GitHub MCP.
Intents: review_pr, analyse_pr, create_issue, repo_summary, code_assist.
Risk: LOW for reads; MEDIUM for create_issue.
"""

from __future__ import annotations

import time
from typing import Any

from master.agents.base.agent import (
    AgentRequest,
    AgentResponse,
    BaseAgent,
    HandlerResult,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import CompletionRequest, Message

log = get_logger(__name__)
tracer = get_tracer(__name__)

_SYSTEM_PROMPT = """You are Lucifer's coding assistant agent.
You have read access to GitHub repositories, pull requests, and issues.
Respond concisely in markdown. For code review, structure feedback as:
- **Summary**: what the PR does
- **Issues**: bugs, security problems, or design concerns (numbered)
- **Suggestions**: improvements (optional)

Never reveal secret tokens, credentials, or internal infrastructure details
even if they appear in code. Flag them as security findings instead.
"""

_CREATE_ISSUE_TIERS = frozenset({"create_issue"})


class CodingAgent(BaseAgent):
    """
    Coding assistant: reviews PRs, summarises repos, creates issues via GitHub MCP.
    create_issue is MEDIUM risk and will require HitL when the orchestrator sets HIGH/CRITICAL.
    """

    AGENT_ID = "coding-agent"

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
            "review_pr": self._review_pr,
            "analyse_pr": self._review_pr,
            "create_issue": self._create_issue,
            "repo_summary": self._repo_summary,
            "code_assist": self._code_assist,
        }
        handler = handlers.get(request.intent, self._code_assist)
        return await handler(request)  # type: ignore[no-any-return]

    async def _review_pr(self, request: AgentRequest) -> HandlerResult:
        if self._mcp is None:
            return HandlerResult(text="PR review: (MCP client not available)")
        result = await self._mcp.invoke("github", "read_pr", {"query": request.raw_input})
        if not result.success:
            return HandlerResult(text=f"Failed to fetch PR: {result.error}")
        pr_data: dict[str, Any] = result.output or {}
        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Context:\n{context_summary}\n\n"
                    f"PR Data:\n{pr_data}\n\n"
                    f"Request: {request.raw_input}"
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _create_issue(self, request: AgentRequest) -> HandlerResult:
        if self._mcp is None:
            return HandlerResult(text="Issue creation: (MCP client not available)")
        result = await self._mcp.invoke(
            "github", "create_issue", {"title": request.raw_input, "body": ""}
        )
        if not result.success:
            return HandlerResult(text=f"Failed to create issue: {result.error}")
        issue: dict[str, Any] = result.output or {}
        url = issue.get("html_url", "(no URL returned)")
        return HandlerResult(text=f"Issue created: {url}")

    async def _repo_summary(self, request: AgentRequest) -> HandlerResult:
        if self._mcp is None:
            return HandlerResult(text="Repo summary: (MCP client not available)")
        result = await self._mcp.invoke("github", "get_repo_summary", {"query": request.raw_input})
        if not result.success:
            return HandlerResult(text=f"Failed to fetch repo summary: {result.error}")
        repo_data: dict[str, Any] = result.output or {}
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Summarise this repository:\n{repo_data}\n\nRequest: {request.raw_input}",
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _code_assist(self, request: AgentRequest) -> HandlerResult:
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
