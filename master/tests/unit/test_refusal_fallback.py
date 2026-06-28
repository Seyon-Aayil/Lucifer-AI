"""B4: client-side refusal fallbacks in ProviderRegistry.complete_with_retry."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.llm.interfaces import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ProviderTier,
    TokenUsage,
)
from master.llm.registry import ProviderRegistry, _is_refusal

_STRONG_ID = "anthropic-claude-opus-4-8"
_WEAK_ID = "anthropic-claude-haiku-4-5"


def _make_provider(provider_id, tier, finish_reason="stop", content="ok"):
    p = MagicMock()
    p.provider_id = provider_id
    p.tier = tier
    p.cost_per_input_token = 0.0
    p.cost_per_output_token = 0.0
    resp = CompletionResponse(
        content=content,
        model=provider_id,
        provider_id=provider_id,
        token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        finish_reason=finish_reason,
    )
    p.complete = AsyncMock(return_value=resp)
    return p


def _registry(weak, strong, refusal_fallback=True):
    return ProviderRegistry(
        providers=[weak, strong],
        strong_model_id=_STRONG_ID,
        refusal_fallback=refusal_fallback,
    )


def _req():
    return CompletionRequest(messages=[Message(role="user", content="hi")], model="x")


# ── _is_refusal ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "finish_reason,expected",
    [("content_filter", True), ("stop", False), ("length", False), ("tool_calls", False)],
)
def test_is_refusal(finish_reason, expected):
    resp = CompletionResponse(
        content="",
        model="m",
        provider_id="p",
        token_usage=TokenUsage(input_tokens=0, output_tokens=0),
        finish_reason=finish_reason,
    )
    assert _is_refusal(resp) is expected


# ── fallback behaviour ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_master_refusal_triggers_fallback():
    weak = _make_provider(_WEAK_ID, ProviderTier.MASTER, "content_filter", "refused")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "stop", "opus answer")
    reg = _registry(weak, strong)

    result = await reg.complete_with_retry(weak, _req())

    strong.complete.assert_awaited_once()
    assert result.content == "opus answer"
    assert result.provider_id == _STRONG_ID


@pytest.mark.asyncio
async def test_local_refusal_does_not_fall_back():
    # DESKTOP (Ollama-tier) refusal must NOT be re-routed to a cloud model.
    weak = _make_provider("ollama-llama3", ProviderTier.DESKTOP, "content_filter", "refused")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "stop", "opus answer")
    reg = _registry(weak, strong)

    result = await reg.complete_with_retry(weak, _req())

    strong.complete.assert_not_awaited()
    assert result.content == "refused"


@pytest.mark.asyncio
async def test_strong_model_refusal_is_terminal():
    weak = _make_provider(_WEAK_ID, ProviderTier.MASTER, "stop")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "content_filter", "refused")
    reg = _registry(weak, strong)

    result = await reg.complete_with_retry(strong, _req())

    # No second strong call beyond the original one.
    strong.complete.assert_awaited_once()
    assert result.content == "refused"


@pytest.mark.asyncio
async def test_fallback_disabled_returns_refusal():
    weak = _make_provider(_WEAK_ID, ProviderTier.MASTER, "content_filter", "refused")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "stop", "opus answer")
    reg = _registry(weak, strong, refusal_fallback=False)

    result = await reg.complete_with_retry(weak, _req())

    strong.complete.assert_not_awaited()
    assert result.content == "refused"


@pytest.mark.asyncio
async def test_non_refusal_no_extra_hop():
    weak = _make_provider(_WEAK_ID, ProviderTier.MASTER, "stop", "fine")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "stop", "opus answer")
    reg = _registry(weak, strong)

    result = await reg.complete_with_retry(weak, _req())

    strong.complete.assert_not_awaited()
    assert result.content == "fine"
