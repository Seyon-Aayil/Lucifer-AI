"""
master.token_optimizer.spend_tracker
======================================
Real-time agent spend tracker.
Redis counters for hot-path daily spend; hourly flush to TimescaleDB.
Raises BudgetExceededError when an agent hits its daily cap.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import asyncpg
from redis.asyncio import Redis

from master.core.exceptions import BudgetExceededError
from master.core.logging import get_logger

log = get_logger(__name__)

_NS = "lucifer:spend"


def _daily_key(agent_id: str, day: date | None = None) -> str:
    d = (day or datetime.now(UTC).date()).isoformat()
    return f"{_NS}:{agent_id}:{d}"


class SpendTracker:
    """
    Tracks per-agent LLM spend in real time using Redis INCRBYFLOAT.
    Compares against limits stored in the `agent_budgets` Postgres table.
    """

    def __init__(self, redis: Redis, db_pool: asyncpg.Pool) -> None:
        self._redis = redis
        self._db = db_pool
        # In-memory cache of daily limits {agent_id: limit_usd}
        self._limits: dict[str, float] = {}

    async def load_limits(self) -> None:
        """Load per-agent daily budget limits from Postgres into memory."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch("SELECT agent_id, daily_limit_usd FROM agent_budgets")
            self._limits = {r["agent_id"]: float(r["daily_limit_usd"]) for r in rows}
        log.info("spend_tracker.limits_loaded", agents=list(self._limits.keys()))

    async def record_spend(self, agent_id: str, cost_usd: float) -> float:
        """
        Record a completed LLM spend for an agent.
        Returns the new cumulative daily total.
        Does NOT enforce the limit — call check_budget() before dispatching.
        """
        key = _daily_key(agent_id)
        new_total: float = await self._redis.incrbyfloat(key, cost_usd)
        # Set TTL of 48h to handle timezone edge cases
        await self._redis.expire(key, 48 * 3600)

        log.info(
            "spend.recorded",
            agent=agent_id,
            cost_usd=round(cost_usd, 6),
            daily_total=round(new_total, 6),
        )
        return new_total

    async def check_budget(self, agent_id: str, estimated_cost: float) -> None:
        """
        Pre-flight budget check before an LLM call.
        Raises BudgetExceededError if this call would push the agent over daily limit.
        """
        limit = self._limits.get(agent_id)
        if limit is None:
            log.warning("spend_tracker.no_limit", agent=agent_id)
            return  # No limit configured — allow (but log)

        key = _daily_key(agent_id)
        raw: bytes | None = await self._redis.get(key)
        current = float(raw) if raw else 0.0

        if current + estimated_cost > limit:
            raise BudgetExceededError(
                agent_id=agent_id,
                limit_usd=limit,
                spent_usd=current + estimated_cost,
            )

    async def get_daily_spend(self, agent_id: str) -> float:
        """Return the current daily spend for an agent."""
        key = _daily_key(agent_id)
        raw: bytes | None = await self._redis.get(key)
        return float(raw) if raw else 0.0

    async def flush_to_timescaledb(self) -> None:
        """
        Hourly flush: write Redis spend totals to `daily_spend` TimescaleDB table.
        Called by APScheduler as a background job.
        """
        today = datetime.now(UTC).date()
        updates: list[tuple[str, float]] = []

        for agent_id in self._limits:
            key = _daily_key(agent_id, today)
            raw = await self._redis.get(key)
            if raw:
                updates.append((agent_id, float(raw)))

        if updates:
            async with self._db.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO daily_spend (agent_id, date, spend_usd)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (agent_id, date) DO UPDATE
                      SET spend_usd = EXCLUDED.spend_usd
                    """,
                    [(agent_id, today, spend) for agent_id, spend in updates],
                )
            log.info("spend_tracker.flushed", agents=len(updates))
