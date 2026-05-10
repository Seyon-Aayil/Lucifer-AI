"""Unit tests for master.core.auth.rate_limit."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.core.auth.rate_limit import FixedWindowRateLimiter


def _redis_with_count(returned_count: int, ttl: int = 30) -> MagicMock:
    redis = MagicMock()
    pipe_cm = MagicMock()
    pipe = MagicMock()
    pipe.incr = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.execute = AsyncMock(return_value=[returned_count, True])
    pipe_cm.__aenter__ = AsyncMock(return_value=pipe)
    pipe_cm.__aexit__ = AsyncMock(return_value=None)
    redis.pipeline = MagicMock(return_value=pipe_cm)
    redis.ttl = AsyncMock(return_value=ttl)
    return redis


def test_constructor_rejects_invalid_limits():
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(MagicMock(), limit=0, window_seconds=60, namespace="x")
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(MagicMock(), limit=5, window_seconds=0, namespace="x")


@pytest.mark.asyncio
async def test_first_attempt_allowed():
    redis = _redis_with_count(1)
    limiter = FixedWindowRateLimiter(redis, limit=5, window_seconds=60, namespace="pair")
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed
    assert decision.remaining == 4
    assert decision.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_at_limit_still_allowed():
    redis = _redis_with_count(5)
    limiter = FixedWindowRateLimiter(redis, limit=5, window_seconds=60, namespace="pair")
    decision = await limiter.check("1.2.3.4")
    assert decision.allowed
    assert decision.remaining == 0


@pytest.mark.asyncio
async def test_over_limit_blocks_and_reports_ttl():
    redis = _redis_with_count(6, ttl=42)
    limiter = FixedWindowRateLimiter(redis, limit=5, window_seconds=60, namespace="pair")
    decision = await limiter.check("1.2.3.4")
    assert not decision.allowed
    assert decision.remaining == 0
    assert decision.retry_after_seconds == 42


@pytest.mark.asyncio
async def test_negative_ttl_falls_back_to_window():
    redis = _redis_with_count(99, ttl=-2)
    limiter = FixedWindowRateLimiter(redis, limit=5, window_seconds=30, namespace="pair")
    decision = await limiter.check("1.2.3.4")
    assert not decision.allowed
    assert decision.retry_after_seconds == 30
