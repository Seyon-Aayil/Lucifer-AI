"""
master.model_upgrade.replay
===========================
Real-traffic replay for model-upgrade shadow evaluation.

`record_query` captures (prompt, response) pairs from live orchestrator turns
into the `query_log` table; `make_replay_loader` samples recent rows into a
replay set (the logged response is the incumbent baseline). When the log is
empty (fresh deployment), it falls back to the built-in golden set so the
scheduler still has something to evaluate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from master.core.logging import get_logger
from master.model_upgrade.golden import GOLDEN_TASKS, GoldenTask
from master.model_upgrade.promoter import ReplayQuery

log = get_logger(__name__)

QueriesLoader = Callable[[str], Awaitable[list[ReplayQuery]]]


async def record_query(
    db_pool: Any, prompt: str, response: str, agent_id: str | None = None
) -> None:
    """Best-effort capture of a real turn for later replay. Never raises."""
    if not prompt or not response:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO query_log (prompt, response, agent_id) VALUES ($1, $2, $3)",
                prompt,
                response,
                agent_id,
            )
    except Exception as exc:  # noqa: BLE001 — capture must not affect the request
        log.warning("model_upgrade.query_log.insert_failed", error=str(exc))


def _golden_fallback(tasks: list[GoldenTask]) -> list[ReplayQuery]:
    # The golden expected answer stands in as the incumbent baseline.
    return [ReplayQuery(t.query_id, t.prompt, t.expected) for t in tasks]


def make_replay_loader(
    db_pool: Any, limit: int = 100, tasks: list[GoldenTask] | None = None
) -> QueriesLoader:
    """
    Build a loader that samples the most recent `limit` query_log rows as the
    replay set. Falls back to the golden set when the log is empty or unreadable.
    """
    fallback_tasks = tasks or GOLDEN_TASKS

    async def _load(_incumbent: str) -> list[ReplayQuery]:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, prompt, response FROM query_log ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
        except Exception as exc:  # noqa: BLE001 — degrade to golden on any error
            log.warning("model_upgrade.replay.load_failed", error=str(exc))
            return _golden_fallback(fallback_tasks)

        if not rows:
            log.info("model_upgrade.replay.empty_log_using_golden")
            return _golden_fallback(fallback_tasks)

        return [
            ReplayQuery(str(r["id"]), r["prompt"], r["response"])
            for r in rows
            if r["prompt"] and r["response"]
        ]

    return _load
