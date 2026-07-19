"""
W6 / ADR-005: session-affinity routing. A session's first-turn model is pinned
so follow-up turns skip RouteLLM reclassification — preserving the provider-side
prompt-cache prefix that a re-route would invalidate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from master.llm.interfaces import ProviderTier
from master.llm.registry import ProviderRegistry

_STRONG = "anthropic-claude-opus-4-8"
_WEAK = "anthropic-claude-haiku-4-5"


def _provider(pid: str, tier: ProviderTier = ProviderTier.MASTER) -> MagicMock:
    p = MagicMock()
    p.provider_id = pid
    p.tier = tier
    p.cost_per_input_token = 1e-6
    p.cost_per_output_token = 1e-6
    p.supports = lambda *caps: True
    return p


def _registry(enabled: bool = True, ttl: int = 3600) -> ProviderRegistry:
    reg = ProviderRegistry(
        providers=[_provider(_STRONG), _provider(_WEAK)],
        strong_model_id=_STRONG,
        weak_model_id=_WEAK,
        session_affinity_enabled=enabled,
        session_affinity_ttl_seconds=ttl,
    )
    # Score high → strong tier; count how often the router actually runs.
    reg._routellm_score = AsyncMock(return_value=0.9)  # type: ignore[method-assign]
    return reg


@pytest.mark.asyncio
async def test_affinity_hit_skips_reclassification() -> None:
    reg = _registry(enabled=True)
    first = await reg.select(query="hi", session_id="s1")
    second = await reg.select(query="thanks!", session_id="s1")

    assert first.provider.provider_id == second.provider.provider_id == _STRONG
    # RouteLLM ran once (first turn); the follow-up reused the pin.
    assert reg._routellm_score.await_count == 1


@pytest.mark.asyncio
async def test_disabled_reclassifies_every_turn() -> None:
    reg = _registry(enabled=False)
    await reg.select(query="hi", session_id="s1")
    await reg.select(query="thanks!", session_id="s1")
    assert reg._routellm_score.await_count == 2


@pytest.mark.asyncio
async def test_distinct_sessions_are_independent() -> None:
    reg = _registry(enabled=True)
    await reg.select(query="hi", session_id="s1")
    await reg.select(query="hi", session_id="s2")
    assert reg._routellm_score.await_count == 2


@pytest.mark.asyncio
async def test_no_session_id_never_pins() -> None:
    reg = _registry(enabled=True)
    await reg.select(query="hi", session_id=None)
    await reg.select(query="hi", session_id=None)
    assert reg._routellm_score.await_count == 2


@pytest.mark.asyncio
async def test_expired_pin_reroutes() -> None:
    reg = _registry(enabled=True, ttl=3600)
    with patch("master.llm.registry.time.monotonic") as mono:
        mono.return_value = 1000.0
        await reg.select(query="hi", session_id="s1")  # pins, expiry = 4600
        mono.return_value = 1000.0 + 3600 + 1  # past TTL
        await reg.select(query="thanks!", session_id="s1")  # must re-route
    assert reg._routellm_score.await_count == 2


@pytest.mark.asyncio
async def test_unhealthy_pin_is_dropped_and_rerouted() -> None:
    reg = _registry(enabled=True)
    await reg.select(query="hi", session_id="s1")  # pins strong

    # Strong goes unhealthy; the pinned provider can no longer proceed.
    reg._breakers[_STRONG].can_proceed = AsyncMock(return_value=False)  # type: ignore[method-assign]
    result = await reg.select(query="again", session_id="s1")

    assert result.provider.provider_id == _WEAK  # fell through to a healthy provider
    # And the session is re-pinned to the healthy provider.
    assert reg._affinity["s1"][0] == _WEAK


@pytest.mark.asyncio
async def test_pin_ignored_when_capabilities_no_longer_met() -> None:
    from master.llm.interfaces import Capability

    reg = _registry(enabled=True)
    await reg.select(query="hi", session_id="s1")  # pins strong (supports all)

    # Now the strong provider stops supporting a newly-required capability.
    reg._providers[_STRONG].supports = lambda *caps: False  # type: ignore[assignment]
    await reg.select(query="hi", session_id="s1", required_capabilities={Capability.VISION})
    # Pin could not be honoured → router ran again.
    assert reg._routellm_score.await_count == 2
