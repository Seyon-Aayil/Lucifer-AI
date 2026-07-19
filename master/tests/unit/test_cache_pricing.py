"""
W8-1 / W8-4: TokenUsage prices all four token classes and reports a cache
hit-rate. The arithmetic is pinned against a LiteLLM-normalised usage fixture
(input_tokens includes cache tokens) rather than assuming the normalisation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from master.llm.interfaces import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CompletionRequest,
    Message,
    TokenUsage,
)
from master.llm.providers.anthropic import AnthropicProvider

_IN = 5.0 / 1_000_000  # opus input rate
_OUT = 25.0 / 1_000_000  # opus output rate


# ── W8-1: cost prices all four classes ────────────────────────────────────────


def test_no_cache_reduces_to_plain_pricing() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=200)
    assert usage.cost(_IN, _OUT) == pytest.approx(1000 * _IN + 200 * _OUT)


def test_cache_tokens_are_not_double_charged() -> None:
    # LiteLLM normalisation: input_tokens (1000) INCLUDES 600 cache-read +
    # 100 cache-write, leaving 300 billed at the full input rate.
    usage = TokenUsage(
        input_tokens=1000, output_tokens=200, cache_read_tokens=600, cache_write_tokens=100
    )
    expected = (
        300 * _IN
        + 600 * _IN * CACHE_READ_MULTIPLIER
        + 100 * _IN * CACHE_WRITE_MULTIPLIER
        + 200 * _OUT
    )
    assert usage.cost(_IN, _OUT) == pytest.approx(expected)


def test_cache_read_is_cheaper_than_full_rate() -> None:
    full = TokenUsage(input_tokens=1000, output_tokens=0)
    cached = TokenUsage(input_tokens=1000, output_tokens=0, cache_read_tokens=1000)
    assert cached.cost(_IN, _OUT) < full.cost(_IN, _OUT)
    # A pure cache-read costs exactly 0.1× the full input price.
    assert cached.cost(_IN, _OUT) == pytest.approx(full.cost(_IN, _OUT) * CACHE_READ_MULTIPLIER)


def test_cache_write_costs_more_than_full_rate() -> None:
    full = TokenUsage(input_tokens=1000, output_tokens=0)
    written = TokenUsage(input_tokens=1000, output_tokens=0, cache_write_tokens=1000)
    assert written.cost(_IN, _OUT) == pytest.approx(full.cost(_IN, _OUT) * CACHE_WRITE_MULTIPLIER)


def test_cache_tokens_exceeding_input_clamp_to_zero_full() -> None:
    # Defensive: never bill negative full-rate tokens on odd normalisation.
    usage = TokenUsage(input_tokens=100, output_tokens=0, cache_read_tokens=1000)
    assert usage.cost(_IN, _OUT) >= 0


# ── W8-4: cache hit-rate ──────────────────────────────────────────────────────


def test_cache_hit_rate_basic() -> None:
    assert TokenUsage(input_tokens=1000, output_tokens=0, cache_read_tokens=300).cache_hit_rate == (
        pytest.approx(0.3)
    )


def test_cache_hit_rate_zero_input() -> None:
    assert TokenUsage(input_tokens=0, output_tokens=10).cache_hit_rate == 0.0


# ── W8-1: normalisation pinned through the real provider mapping ──────────────


def _fake_litellm_response(
    prompt_tokens: int, cache_read: int, cache_write: int
) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,  # LiteLLM: INCLUDES cache read + write
        completion_tokens=50,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    message = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


@pytest.mark.asyncio
async def test_provider_maps_litellm_cache_fields_and_prices_them() -> None:
    provider = AnthropicProvider(
        model="claude-opus-4-8", litellm_proxy_url="http://x", litellm_api_key="k"
    )
    request = CompletionRequest(messages=[Message(role="user", content="hi")], model="m")
    fixture = _fake_litellm_response(prompt_tokens=1000, cache_read=600, cache_write=100)

    with patch(
        "master.llm.providers.anthropic.litellm.acompletion",
        new=AsyncMock(return_value=fixture),
    ):
        resp = await provider.complete(request)

    tu = resp.token_usage
    # Mapping: prompt_tokens → input_tokens (cache-inclusive), separate cache fields.
    assert tu.input_tokens == 1000
    assert tu.cache_read_tokens == 600
    assert tu.cache_write_tokens == 100
    assert tu.cache_hit_rate == pytest.approx(0.6)
    # Cost prices the cached tokens at their own rates, not the full input rate.
    expected = 300 * _IN + 600 * _IN * 0.1 + 100 * _IN * 1.25 + 50 * _OUT
    assert tu.cost(_IN, _OUT) == pytest.approx(expected)
