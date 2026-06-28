"""Live web search wiring for ResearchAgent (via the web_search MCP server)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from master.agents.base.agent import AgentRequest, AgentSurface, ContextPackage, RiskTier
from master.agents.research.agent import ResearchAgent
from master.llm.interfaces import CompletionResponse, ProviderSelection, TokenUsage

_REPO = Path(__file__).resolve().parents[3]


def _mock_registry():
    provider = MagicMock()
    provider.provider_id = "anthropic-claude-opus-4-8"
    provider.cost_per_input_token = 1e-6
    provider.cost_per_output_token = 2e-6
    registry = MagicMock()
    registry.select = AsyncMock(return_value=ProviderSelection(provider=provider, effort=None))
    registry.complete_with_retry = AsyncMock(
        return_value=CompletionResponse(
            content="synthesised answer",
            model="claude-opus-4-8",
            provider_id=provider.provider_id,
            token_usage=TokenUsage(input_tokens=100, output_tokens=50),
            finish_reason="stop",
        )
    )
    return registry


def _mcp_with(web=None, notion=None):
    """MCP client whose invoke() dispatches canned results per (server, tool)."""
    mcp = MagicMock()

    async def _invoke(server_id, tool_name, args):
        if server_id == "web_search":
            return web if web is not None else SimpleNamespace(success=False, output=None)
        if server_id == "notion":
            return notion if notion is not None else SimpleNamespace(success=True, output=[])
        return SimpleNamespace(success=False, output=None)

    mcp.invoke = AsyncMock(side_effect=_invoke)
    return mcp


def _agent(registry, mcp):
    return ResearchAgent(
        agent_id=ResearchAgent.AGENT_ID,
        librarian=MagicMock(),
        llm_registry=registry,
        telemetry_emitter=MagicMock(),
        mcp_client=mcp,
    )


def _request(intent: str) -> AgentRequest:
    return AgentRequest.create(
        agent_id="research-agent",
        intent=intent,
        raw_input="what is the latest on X",
        surface=AgentSurface.WEB,
        context_package=ContextPackage(requesting_agent="research-agent", task_type=intent),
        risk_tier=RiskTier.LOW,
    )


_WEB_OK = SimpleNamespace(
    success=True,
    output=[
        {"title": "Result One", "url": "https://a.example/1", "snippet": "first hit"},
        {"title": "Result Two", "url": "https://b.example/2", "snippet": "second hit"},
    ],
)


@pytest.mark.asyncio
async def test_research_includes_web_results_in_prompt():
    registry = _mock_registry()
    agent = _agent(registry, _mcp_with(web=_WEB_OK))

    resp = await agent.execute(_request("research"))

    # The web search was invoked and its results reached the LLM prompt.
    agent._mcp.invoke.assert_any_await(
        "web_search", "search", {"query": "what is the latest on X", "max_results": 5}
    )
    sent = registry.complete_with_retry.await_args.args[1]  # CompletionRequest
    user_msg = sent.messages[-1].content
    assert "Web search results:" in user_msg
    assert "Result One" in user_msg and "https://a.example/1" in user_msg
    # Real token usage propagated (Phase-3 plumbing).
    assert resp.token_usage.total_tokens == 150


@pytest.mark.asyncio
async def test_web_search_intent_uses_web_first():
    registry = _mock_registry()
    agent = _agent(registry, _mcp_with(web=_WEB_OK))

    await agent.execute(_request("web_search"))

    sent = registry.complete_with_retry.await_args.args[1]
    assert "Result Two" in sent.messages[-1].content


@pytest.mark.asyncio
async def test_web_search_falls_back_when_no_hits():
    registry = _mock_registry()
    # web_search returns failure → _web_search falls back to _research (Notion+LLM).
    agent = _agent(registry, _mcp_with(web=SimpleNamespace(success=False, output=None)))

    resp = await agent.execute(_request("web_search"))

    # Still produced an answer via the LLM fallback path.
    assert resp.result["content"] == "synthesised answer"


@pytest.mark.asyncio
async def test_web_search_results_empty_without_mcp():
    agent = _agent(_mock_registry(), mcp=None)
    assert await agent._web_search_results(_request("research")) == ""


# ── Two-layer ACL must stay consistent ────────────────────────────────────────


def test_web_search_server_allows_research_agent():
    data = yaml.safe_load((_REPO / "infra" / "mcp_servers.yaml").read_text())
    server = next(s for s in data["servers"] if s["server_id"] == "web_search")
    assert "research-agent" in server["allowed_agents"]
    assert any(t["name"] == "search" for t in server["tools"])


def test_research_manifest_grants_web_search():
    manifest = json.loads(
        (_REPO / "master" / "agents" / "research" / "agent_manifest.json").read_text()
    )
    assert manifest["allowed_tools"].get("web_search") == ["search"]
