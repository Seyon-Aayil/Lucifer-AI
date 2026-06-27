"""Phase 3 close-out: agents propagate real LLM token usage + cost; financial summary live."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from master.agents.base.agent import AgentRequest, AgentSurface, ContextPackage, RiskTier
from master.agents.financial.agent import FinancialAgent
from master.agents.personal.agent import PersonalAgent
from master.llm.interfaces import CompletionResponse, ProviderSelection, TokenUsage


def _mock_registry(content="answer", in_tokens=100, out_tokens=50):
    """Registry whose select→provider and complete_with_retry→response are canned."""
    provider = MagicMock()
    provider.provider_id = "anthropic-claude-opus-4-8"
    provider.cost_per_input_token = 1e-6
    provider.cost_per_output_token = 2e-6

    registry = MagicMock()
    registry.select = AsyncMock(return_value=ProviderSelection(provider=provider, effort=None))
    registry.complete_with_retry = AsyncMock(
        return_value=CompletionResponse(
            content=content,
            model="claude-opus-4-8",
            provider_id=provider.provider_id,
            token_usage=TokenUsage(input_tokens=in_tokens, output_tokens=out_tokens),
            finish_reason="stop",
        )
    )
    return registry, provider


def _make(agent_cls, registry, mcp=None):
    return agent_cls(
        agent_id=agent_cls.AGENT_ID,
        librarian=MagicMock(),
        llm_registry=registry,
        telemetry_emitter=MagicMock(),
        mcp_client=mcp,
    )


def _request(intent: str) -> AgentRequest:
    return AgentRequest.create(
        agent_id="x",
        intent=intent,
        raw_input="hello",
        surface=AgentSurface.WEB,
        context_package=ContextPackage(requesting_agent="x", task_type=intent),
        risk_tier=RiskTier.LOW,
    )


@pytest.mark.asyncio
async def test_llm_turn_reports_token_usage_and_cost():
    registry, _ = _mock_registry(in_tokens=100, out_tokens=50)
    agent = _make(PersonalAgent, registry)

    resp = await agent.execute(_request("chat"))

    assert resp.token_usage.input_tokens == 100
    assert resp.token_usage.output_tokens == 50
    # cost = 100 * 1e-6 + 50 * 2e-6 = 2e-4
    assert resp.cost_usd == pytest.approx(2e-4)
    assert resp.result["content"] == "answer"


@pytest.mark.asyncio
async def test_pure_mcp_turn_reports_zero_usage():
    registry, _ = _mock_registry()
    agent = _make(PersonalAgent, registry, mcp=None)  # daily_briefing makes no LLM call
    with patch("master.agents.librarian.graph_client.GraphClient.from_settings") as gc:
        gc.return_value.close = AsyncMock()
        gc.return_value.list_nodes_by_type = AsyncMock(return_value=[])

        resp = await agent.execute(_request("daily_briefing"))

    assert resp.token_usage.input_tokens == 0
    assert resp.cost_usd == 0.0
    registry.complete_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_financial_spending_summary_uses_live_data_not_placeholder():
    registry, _ = _mock_registry()
    agent = _make(FinancialAgent, registry)
    nodes = [
        {"kind": "budget", "amount": 1000},
        {"kind": "transaction", "amount": 40, "category": "food"},
        {"kind": "spend", "amount": 60, "category": "transport"},
    ]
    with patch("master.agents.librarian.graph_client.GraphClient.from_settings") as gc:
        gc.return_value.close = AsyncMock()
        gc.return_value.list_nodes_by_type = AsyncMock(return_value=nodes)

        resp = await agent.execute(_request("spending_summary"))

    # The LLM was called with aggregated spend data, not the old placeholder.
    sent = registry.complete_with_retry.await_args.args[1]  # completion_req
    user_msg = sent.messages[-1].content
    assert "pending Phase 3" not in user_msg
    assert "total spend" in user_msg
    assert "food" in user_msg and "transport" in user_msg
    # Real usage propagated.
    assert resp.token_usage.total_tokens == 150
