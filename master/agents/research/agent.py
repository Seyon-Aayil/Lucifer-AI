"""
master.agents.research.agent
==============================
ResearchAgent: web search, Notion knowledge base queries, and summarisation.

Tools: Notion MCP, GitHub MCP (repo/PR context).
Intents: research, search_notion, repo_summary, summarise, web_search.
Risk: LOW for all intents (read-only).
"""

from __future__ import annotations

import time
from typing import Any

from master.agents.base.agent import (
    AgentRequest,
    AgentResponse,
    BaseAgent,
    MemoryDelta,
    TokenUsage,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import CompletionRequest, Message

log = get_logger(__name__)
tracer = get_tracer(__name__)

_SYSTEM_PROMPT = """You are Lucifer's research assistant agent.
You retrieve and synthesise information from Notion and GitHub.
Rules:
- Cite sources when you have them (page title, URL, or PR number).
- Summarise clearly; avoid padding.
- If asked for web search results, note that live internet search is not yet available
  and offer to search Notion or GitHub instead.
- Classify any retrieved content before including it: Restricted/Secret content
  must not be quoted even if retrieved from Notion.
"""


class ResearchAgent(BaseAgent):
    """
    Research assistant: searches Notion pages, queries databases, summarises content.
    """

    AGENT_ID = "research-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:
        with tracer.start_as_current_span(f"agent.{self.AGENT_ID}.execute"):
            self._emit("agent.start", request, intent=request.intent)
            start = time.monotonic()

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
            "research": self._research,
            "search_notion": self._search_notion,
            "repo_summary": self._repo_summary,
            "summarise": self._summarise,
            "web_search": self._web_search_fallback,
        }
        handler = handlers.get(request.intent, self._research)
        return await handler(request)  # type: ignore[no-any-return]

    async def _search_notion(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        if self._mcp is None:
            return "Notion search: (MCP client not available)", []
        result = await self._mcp.invoke("notion", "search_pages", {"query": request.raw_input})
        if not result.success:
            return f"Notion search failed: {result.error}", []
        pages: list[dict[str, Any]] = result.output or []
        if not pages:
            return "No Notion pages found for that query.", []
        lines = [f"- [{p.get('title', '(untitled)')}]({p.get('url', '#')})" for p in pages[:10]]
        return "## Notion Results\n\n" + "\n".join(lines), []

    async def _repo_summary(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        if self._mcp is None:
            return "Repo summary: (MCP client not available)", []
        result = await self._mcp.invoke("github", "get_repo_summary", {"query": request.raw_input})
        if not result.success:
            return f"Repo summary failed: {result.error}", []
        repo_data: dict[str, Any] = result.output or {}
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=f"Summarise this repo:\n{repo_data}"),
        ]
        return await self._llm_complete(request, messages)

    async def _summarise(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        if self._mcp is None:
            return await self._llm_chat(request)
        # Try to fetch the Notion page first, then summarise with LLM
        result = await self._mcp.invoke("notion", "read_page", {"query": request.raw_input})
        if result.success and result.output:
            page_content = result.output
            messages = [
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=f"Summarise this page:\n{page_content}\n\nRequest: {request.raw_input}",
                ),
            ]
            return await self._llm_complete(request, messages)
        return await self._llm_chat(request)

    async def _research(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """General research: try Notion search, then LLM with context."""
        notion_context = ""
        if self._mcp is not None:
            result = await self._mcp.invoke("notion", "search_pages", {"query": request.raw_input})
            if result.success and result.output:
                pages: list[dict[str, Any]] = result.output[:5]
                notion_context = "\n".join(
                    f"- {p.get('title', '(untitled)')}: {p.get('url', '')}" for p in pages
                )

        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Context:\n{context_summary}\n\n"
                    + (f"Notion pages found:\n{notion_context}\n\n" if notion_context else "")
                    + f"Research request: {request.raw_input}"
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _web_search_fallback(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
        """Live web search not available yet; fall back to Notion + LLM."""
        log.info("research_agent.web_search_fallback", agent=self.AGENT_ID)
        return await self._research(request)

    async def _llm_chat(self, request: AgentRequest) -> tuple[str, list[MemoryDelta]]:
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
            temperature=0.5,
        )
        response = await self._llm.complete_with_retry(provider, completion_req)
        return response.content, []
