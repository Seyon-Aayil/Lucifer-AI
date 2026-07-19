"""
Regression: HealthAgent must force LOCAL inference — health data may never reach
a cloud API. It selects by the DESKTOP tier (Ollama), not a non-existent
`preferred_provider` kwarg (which raised TypeError on this path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.agents.base.agent import AgentRequest, AgentSurface, ContextPackage, RiskTier
from master.agents.health.agent import HealthAgent
from master.llm.interfaces import (
    CompletionResponse,
    ProviderSelection,
    ProviderTier,
    TokenUsage,
)


def _registry() -> tuple[MagicMock, MagicMock]:
    provider = MagicMock()
    provider.provider_id = "ollama-llama3"
    provider.tier = ProviderTier.DESKTOP
    provider.cost_per_input_token = 0.0
    provider.cost_per_output_token = 0.0
    registry = MagicMock()
    registry.select = AsyncMock(return_value=ProviderSelection(provider=provider, effort=None))
    registry.complete_with_retry = AsyncMock(
        return_value=CompletionResponse(
            content="local answer",
            model="llama3",
            provider_id="ollama-llama3",
            token_usage=TokenUsage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
        )
    )
    return registry, provider


def _request() -> AgentRequest:
    return AgentRequest.create(
        agent_id="health-agent",
        intent="health_query",
        raw_input="how did I sleep?",
        surface=AgentSurface.WEB,
        context_package=ContextPackage(requesting_agent="health-agent", task_type="health_query"),
        risk_tier=RiskTier.LOW,
    )


@pytest.mark.asyncio
async def test_health_query_selects_desktop_tier() -> None:
    registry, _ = _registry()
    agent = HealthAgent(
        agent_id="health-agent",
        librarian=MagicMock(),
        llm_registry=registry,
        telemetry_emitter=MagicMock(),
        mcp_client=None,
    )

    resp = await agent.execute(_request())

    assert resp.status == "success"
    # The call succeeds (no TypeError) and forces the local tier.
    kwargs = registry.select.await_args.kwargs
    assert kwargs["tier"] == ProviderTier.DESKTOP
    assert "preferred_provider" not in kwargs
