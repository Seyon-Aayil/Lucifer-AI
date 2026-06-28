"""Unit tests for AnthropicProvider: pricing (B1) and sampling-param gating (B2)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from master.llm.interfaces import CompletionRequest, Message
from master.llm.providers.anthropic import _MODEL_COSTS, AnthropicProvider


def _fake_response() -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    message = SimpleNamespace(content="hello", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


def _provider(model: str) -> AnthropicProvider:
    return AnthropicProvider(model=model, litellm_proxy_url="http://x", litellm_api_key="k")


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role="user", content="hi")],
        model="ignored",
        max_tokens=64,
        temperature=0.7,
    )


# ── B1: pricing ───────────────────────────────────────────────────────────────


def test_model_costs_keys_are_current():
    assert set(_MODEL_COSTS) == {
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    }


def test_opus_pricing_corrected():
    p = _provider("claude-opus-4-8")
    assert p.cost_per_input_token == 5.0 / 1_000_000
    assert p.cost_per_output_token == 25.0 / 1_000_000


def test_haiku_pricing_corrected():
    p = _provider("claude-haiku-4-5")
    assert p.cost_per_input_token == 1.0 / 1_000_000
    assert p.cost_per_output_token == 5.0 / 1_000_000


# ── B2: sampling-param gating ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_omits_temperature_for_opus_4_8():
    p = _provider("claude-opus-4-8")
    with patch(
        "master.llm.providers.anthropic.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response()),
    ) as mock_acompletion:
        await p.complete(_request())

    kwargs = mock_acompletion.call_args.kwargs
    assert "temperature" not in kwargs
    assert kwargs["model"] == "anthropic/claude-opus-4-8"


@pytest.mark.asyncio
async def test_complete_keeps_temperature_for_haiku():
    p = _provider("claude-haiku-4-5")
    with patch(
        "master.llm.providers.anthropic.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response()),
    ) as mock_acompletion:
        await p.complete(_request())

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["temperature"] == 0.7
