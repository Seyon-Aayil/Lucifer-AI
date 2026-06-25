"""B3 effort-as-routing + B5 structured outputs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from master.agents.base.agent import RiskTier
from master.llm.interfaces import (
    Capability,
    CompletionRequest,
    Message,
    ProviderSelection,
    ProviderTier,
)
from master.llm.providers.anthropic import AnthropicProvider
from master.llm.registry import ProviderRegistry
from master.model_upgrade.judge import parse_judge_score
from master.orchestrator.schemas import IntentClassification

# ── B3: complexity → effort mapping ───────────────────────────────────────────


def _registry() -> ProviderRegistry:
    return ProviderRegistry(providers=[], routellm_threshold=0.5)


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, None),
        (0.49, None),
        (0.5, "medium"),
        (0.69, "medium"),
        (0.7, "high"),
        (0.84, "high"),
        (0.85, "xhigh"),
        (1.0, "xhigh"),
    ],
)
def test_complexity_to_effort(score, expected):
    assert _registry().complexity_to_effort(score) == expected


class _FakeProvider:
    provider_id = "anthropic-claude-opus-4-8"
    tier = ProviderTier.MASTER
    cost_per_input_token = 1e-9

    def supports(self, *caps: Capability) -> bool:
        return True


@pytest.mark.asyncio
async def test_select_returns_provider_selection_with_effort():
    reg = ProviderRegistry(providers=[_FakeProvider()], routellm_threshold=0.5)  # type: ignore[list-item]
    with patch.object(reg, "_routellm_score", new=AsyncMock(return_value=0.9)):
        sel = await reg.select(query="complex task", tier=ProviderTier.MASTER)
    assert isinstance(sel, ProviderSelection)
    assert sel.provider.provider_id == "anthropic-claude-opus-4-8"
    assert sel.effort == "xhigh"


# ── B3: provider forwards effort + adaptive thinking, model-gated ─────────────


def _fake_response() -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content="hi", tool_calls=None), finish_reason="stop"
    )
    return SimpleNamespace(usage=usage, choices=[choice])


def _provider(model: str) -> AnthropicProvider:
    return AnthropicProvider(model=model, litellm_proxy_url="http://x", litellm_api_key="k")


async def _capture(provider: AnthropicProvider, request: CompletionRequest) -> dict:
    with patch(
        "master.llm.providers.anthropic.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response()),
    ) as mock:
        await provider.complete(request)
    return mock.call_args.kwargs


@pytest.mark.asyncio
async def test_effort_and_thinking_forwarded_for_opus():
    req = CompletionRequest(messages=[Message(role="user", content="hi")], model="x", effort="high")
    kwargs = await _capture(_provider("claude-opus-4-8"), req)
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_effort_omitted_for_haiku():
    req = CompletionRequest(messages=[Message(role="user", content="hi")], model="x", effort="high")
    kwargs = await _capture(_provider("claude-haiku-4-5"), req)
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs


# ── B5: provider forwards response_format, model-gated ────────────────────────


@pytest.mark.asyncio
async def test_response_format_forwarded_when_schema_set():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    req = CompletionRequest(
        messages=[Message(role="user", content="hi")], model="x", response_schema=schema
    )
    # Haiku 4.5 supports structured outputs (but not effort).
    kwargs = await _capture(_provider("claude-haiku-4-5"), req)
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["schema"] == schema


@pytest.mark.asyncio
async def test_response_format_omitted_when_no_schema():
    req = CompletionRequest(messages=[Message(role="user", content="hi")], model="x")
    kwargs = await _capture(_provider("claude-opus-4-8"), req)
    assert "response_format" not in kwargs


# ── B5: judge parses structured score, falls back to free text ────────────────


def test_judge_parses_structured_json():
    assert parse_judge_score('{"score": 8}') == pytest.approx(0.8)


def test_judge_falls_back_to_free_text():
    assert parse_judge_score("I'd rate this a 7 out of 10") == pytest.approx(0.7)


def test_judge_garbage_scores_zero():
    assert parse_judge_score("no numbers here") == 0.0


def test_judge_clamps_out_of_range():
    assert parse_judge_score('{"score": 15}') == 1.0


# ── B5: intent classification schema ──────────────────────────────────────────


def test_intent_schema_valid():
    parsed = IntentClassification.model_validate(
        {"intent": "research", "agent_id": "research-agent", "risk_tier": "low"}
    )
    assert parsed.intent == "research"
    assert parsed.risk_tier is RiskTier.LOW


def test_intent_schema_coerces_unknown_risk_to_low():
    parsed = IntentClassification.model_validate(
        {"intent": "x", "agent_id": "y", "risk_tier": "bogus", "extra": 1}
    )
    assert parsed.risk_tier is RiskTier.LOW  # invalid risk → LOW, extra key ignored


def test_intent_schema_defaults():
    parsed = IntentClassification.model_validate({})
    assert parsed.intent == "chat"
    assert parsed.agent_id == "personal-agent"
    assert parsed.risk_tier is RiskTier.LOW
