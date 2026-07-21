"""
W8-4 (Gemini): the Google provider maps LiteLLM's normalised cache-token usage
and prices it with Gemini's economics (cache read ≈0.25× the input rate, no
per-token creation premium) — not Anthropic's 0.1×/1.25×, and no longer at the
full input rate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from master.llm.interfaces import (
    CACHE_READ_MULTIPLIER,
    CompletionRequest,
    Message,
    ProviderTier,
    TokenUsage,
)
from master.llm.providers.anthropic import AnthropicProvider
from master.llm.providers.google import GoogleProvider

_G_IN = 1.25 / 1_000_000  # gemini-2.5-pro input rate
_G_OUT = 10.0 / 1_000_000


def _google() -> GoogleProvider:
    return GoogleProvider(model="gemini-2.5-pro", litellm_proxy_url="http://x", litellm_api_key="k")


def _fake_response(prompt_tokens: int, cache_read: int) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,  # LiteLLM: includes cached tokens
        completion_tokens=40,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0,  # Gemini has no per-token creation count
    )
    message = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


# ── multiplier wiring ─────────────────────────────────────────────────────────


def test_gemini_multipliers_differ_from_anthropic_defaults() -> None:
    g = _google()
    assert g.cache_read_multiplier == pytest.approx(0.25)
    assert g.cache_write_multiplier == pytest.approx(1.0)


def test_anthropic_keeps_default_multipliers() -> None:
    a = AnthropicProvider(
        model="claude-opus-4-8", litellm_proxy_url="http://x", litellm_api_key="k"
    )
    assert a.cache_read_multiplier == pytest.approx(CACHE_READ_MULTIPLIER)  # 0.1×
    assert a.cache_write_multiplier == pytest.approx(1.25)


def test_gemini_is_master_tier() -> None:
    assert _google().tier == ProviderTier.MASTER


# ── token mapping + pricing through the real provider path ────────────────────


@pytest.mark.asyncio
async def test_gemini_maps_and_prices_cache_reads() -> None:
    provider = _google()
    request = CompletionRequest(messages=[Message(role="user", content="hi")], model="m")
    fixture = _fake_response(prompt_tokens=1000, cache_read=800)

    with patch(
        "master.llm.providers.google.litellm.acompletion",
        new=AsyncMock(return_value=fixture),
    ):
        resp = await provider.complete(request)

    tu = resp.token_usage
    assert tu.input_tokens == 1000
    assert tu.cache_read_tokens == 800  # previously dropped → billed at full rate
    assert tu.cache_hit_rate == pytest.approx(0.8)

    # 200 tokens at full input rate + 800 cache-read at 0.25× + output at out rate.
    expected = 200 * _G_IN + 800 * _G_IN * 0.25 + 40 * _G_OUT
    priced = tu.cost(
        _G_IN,
        _G_OUT,
        cache_read_multiplier=provider.cache_read_multiplier,
        cache_write_multiplier=provider.cache_write_multiplier,
    )
    assert priced == pytest.approx(expected)


def test_gemini_cache_read_cheaper_than_full_but_pricier_than_anthropic() -> None:
    # A Gemini cache read (0.25×) is cheaper than full rate but not as cheap as
    # Anthropic's 0.1× — the per-provider multiplier is what makes this correct.
    usage = TokenUsage(input_tokens=1000, output_tokens=0, cache_read_tokens=1000)
    full = usage.cost(_G_IN, _G_OUT, cache_read_multiplier=1.0)
    gemini = usage.cost(_G_IN, _G_OUT, cache_read_multiplier=0.25)
    anthropic = usage.cost(_G_IN, _G_OUT, cache_read_multiplier=0.1)
    assert anthropic < gemini < full
