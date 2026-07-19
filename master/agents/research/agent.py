"""
master.agents.research.agent
==============================
ResearchAgent: live web search, Notion knowledge base queries, and summarisation.

Tools: Web Search MCP, Notion MCP, GitHub MCP (repo/PR context).
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
    HandlerResult,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import CompletionRequest, Message

log = get_logger(__name__)
tracer = get_tracer(__name__)

_SYSTEM_PROMPT = """You are Lucifer's research assistant agent.
You retrieve and synthesise information from live web search, Notion, and GitHub.
Rules:
- Cite sources when you have them (web page title + URL, Notion page title, or PR number).
- Summarise clearly; avoid padding.
- Prefer the provided web search results for current/factual questions; say so if no
  results were available rather than inventing sources.
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
            "research": self._research,
            "search_notion": self._search_notion,
            "repo_summary": self._repo_summary,
            "summarise": self._summarise,
            "web_search": self._web_search,
        }
        handler = handlers.get(request.intent, self._research)
        return await handler(request)  # type: ignore[no-any-return]

    async def _search_notion(self, request: AgentRequest) -> HandlerResult:
        if self._mcp is None:
            return HandlerResult(text="Notion search: (MCP client not available)")
        result = await self._mcp.invoke("notion", "search_pages", {"query": request.raw_input})
        if not result.success:
            return HandlerResult(text=f"Notion search failed: {result.error}")
        pages: list[dict[str, Any]] = result.output or []
        if not pages:
            return HandlerResult(text="No Notion pages found for that query.")
        lines = [f"- [{p.get('title', '(untitled)')}]({p.get('url', '#')})" for p in pages[:10]]
        return HandlerResult(text="## Notion Results\n\n" + "\n".join(lines))

    async def _repo_summary(self, request: AgentRequest) -> HandlerResult:
        if self._mcp is None:
            return HandlerResult(text="Repo summary: (MCP client not available)")
        result = await self._mcp.invoke("github", "get_repo_summary", {"query": request.raw_input})
        if not result.success:
            return HandlerResult(text=f"Repo summary failed: {result.error}")
        repo_data: dict[str, Any] = result.output or {}
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=f"Summarise this repo:\n{repo_data}"),
        ]
        return await self._llm_complete(request, messages)

    async def _summarise(self, request: AgentRequest) -> HandlerResult:
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

    async def _web_search_results(self, request: AgentRequest) -> str:
        """
        Run a live web search via the web_search MCP server and return a
        formatted result block. Returns "" when unavailable (no MCP client,
        tool failure, or no hits) — never raises.
        """
        if self._mcp is None:
            return ""
        try:
            result = await self._mcp.invoke(
                "web_search", "search", {"query": request.raw_input, "max_results": 5}
            )
        except Exception as exc:
            log.warning("research_agent.web_search_failed", error=str(exc))
            return ""
        if not result.success or not result.output:
            return ""
        hits: list[dict[str, Any]] = result.output if isinstance(result.output, list) else []
        lines = [
            f"- {h.get('title', '(untitled)')} ({h.get('url', '')}): {h.get('snippet', '')}"
            for h in hits[:5]
        ]
        return "\n".join(lines)

    async def _research(self, request: AgentRequest) -> HandlerResult:
        """General research: live web search + Notion search, then synthesise via LLM."""
        web_context = await self._web_search_results(request)

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
                    + (f"Web search results:\n{web_context}\n\n" if web_context else "")
                    + (f"Notion pages found:\n{notion_context}\n\n" if notion_context else "")
                    + f"Research request: {request.raw_input}"
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _web_search(self, request: AgentRequest) -> HandlerResult:
        """Web-search-first research; falls back to Notion + LLM when no web hits."""
        web_context = await self._web_search_results(request)
        if not web_context:
            log.info("research_agent.web_search_empty_fallback", agent=self.AGENT_ID)
            return await self._research(request)

        context_summary = request.context_package.summary or ""
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    f"Context:\n{context_summary}\n\n"
                    f"Web search results:\n{web_context}\n\n"
                    f"Answer using the web results above and cite the URLs: {request.raw_input}"
                ),
            ),
        ]
        return await self._llm_complete(request, messages)

    async def _llm_chat(self, request: AgentRequest) -> HandlerResult:
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
            session_id=request.session_id,
        )
        provider = selection.provider
        completion_req = CompletionRequest(
            messages=messages,
            model=provider.provider_id.split("-", 1)[-1],
            max_tokens=request.token_budget.output_limit,
            effort=selection.effort,
        )
        return await self._run_llm(provider, completion_req)
