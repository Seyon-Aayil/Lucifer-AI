"""
Integration tests for FixedWindowRateLimiter against a real Redis.

Validates the INCR + EXPIRE window behaviour (pipelined) that the unit suite
mocks out. Skips when Redis is unreachable.
"""

from __future__ import annotations

import os
import uuid

import pytest

aioredis = pytest.importorskip("redis.asyncio")

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — unreachable means skip
        pytest.skip(f"Redis not reachable for integration tests: {exc}")
    try:
        yield client
    finally:
        await client.aclose()


async def test_limit_enforced_within_window(redis_client) -> None:
    from master.core.auth.rate_limit import FixedWindowRateLimiter

    ns = f"itest-{uuid.uuid4().hex}"
    limiter = FixedWindowRateLimiter(redis_client, limit=3, window_seconds=60, namespace=ns)
    key = "203.0.113.7"
    try:
        for expected_remaining in (2, 1, 0):
            decision = await limiter.check(key)
            assert decision.allowed is True
            assert decision.remaining == expected_remaining
            assert decision.retry_after_seconds == 0

        blocked = await limiter.check(key)
        assert blocked.allowed is False
        assert blocked.remaining == 0
        assert 1 <= blocked.retry_after_seconds <= 60
    finally:
        await redis_client.delete(f"lucifer:ratelimit:{ns}:{key}")


async def test_separate_keys_have_separate_windows(redis_client) -> None:
    from master.core.auth.rate_limit import FixedWindowRateLimiter

    ns = f"itest-{uuid.uuid4().hex}"
    limiter = FixedWindowRateLimiter(redis_client, limit=1, window_seconds=60, namespace=ns)
    try:
        assert (await limiter.check("ip-a")).allowed is True
        assert (await limiter.check("ip-a")).allowed is False
        # A different key is unaffected by ip-a's exhausted window.
        assert (await limiter.check("ip-b")).allowed is True
    finally:
        await redis_client.delete(f"lucifer:ratelimit:{ns}:ip-a", f"lucifer:ratelimit:{ns}:ip-b")
