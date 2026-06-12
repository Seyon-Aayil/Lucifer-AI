"""
Integration tests for SpendTracker daily counters against a real Redis.

Postgres is mocked out — `load_limits` is the only DB path; seeding `_limits`
directly keeps these tests Redis-only. Skips when Redis is unreachable.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

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


async def test_record_spend_accumulates(redis_client) -> None:
    from master.token_optimizer.spend_tracker import SpendTracker, _daily_key

    agent = "itest-spend-accumulate"
    tracker = SpendTracker(redis=redis_client, db_pool=MagicMock())
    tracker._limits = {agent: 1.0}
    try:
        assert await tracker.record_spend(agent, 0.25) == pytest.approx(0.25)
        assert await tracker.record_spend(agent, 0.50) == pytest.approx(0.75)
        assert await tracker.get_daily_spend(agent) == pytest.approx(0.75)
    finally:
        await redis_client.delete(_daily_key(agent))


async def test_check_budget_blocks_over_limit(redis_client) -> None:
    from master.core.exceptions import BudgetExceededError
    from master.token_optimizer.spend_tracker import SpendTracker, _daily_key

    agent = "itest-spend-budget"
    tracker = SpendTracker(redis=redis_client, db_pool=MagicMock())
    tracker._limits = {agent: 1.0}
    try:
        await tracker.record_spend(agent, 0.9)
        await tracker.check_budget(agent, 0.05)  # 0.95 < 1.0 — allowed
        with pytest.raises(BudgetExceededError):
            await tracker.check_budget(agent, 0.2)  # 1.1 > 1.0 — blocked
    finally:
        await redis_client.delete(_daily_key(agent))


async def test_unconfigured_agent_is_allowed(redis_client) -> None:
    from master.token_optimizer.spend_tracker import SpendTracker

    tracker = SpendTracker(redis=redis_client, db_pool=MagicMock())
    # No limit configured — check passes (logged, not enforced).
    await tracker.check_budget("itest-unknown-agent", 100.0)
