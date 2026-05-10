"""Unit tests for master.agents.librarian.decay_scheduler."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from master.agents.librarian.decay_scheduler import (
    _DECAY_RATE,
    _DELETE_THRESHOLD,
    DecayScheduler,
    _parse_dt,
)


# ── _parse_dt ─────────────────────────────────────────────────────────────────


def test_parse_dt_none_returns_none():
    assert _parse_dt(None) is None


def test_parse_dt_aware_datetime_passthrough():
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _parse_dt(dt) is dt


def test_parse_dt_naive_datetime_gets_utc():
    naive = datetime(2026, 1, 1, 12, 0)
    result = _parse_dt(naive)
    assert result.tzinfo is UTC


def test_parse_dt_iso_string():
    result = _parse_dt("2026-01-01T12:00:00+00:00")
    assert result == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_parse_dt_iso_string_naive_gets_utc():
    result = _parse_dt("2026-01-01T12:00:00")
    assert result.tzinfo is UTC


def test_parse_dt_invalid_returns_none():
    assert _parse_dt("not-a-date") is None
    assert _parse_dt("") is None


# ── DecayScheduler.attach ─────────────────────────────────────────────────────


def test_attach_registers_cron_job():
    gc = MagicMock()
    scheduler_obj = MagicMock()
    ds = DecayScheduler(gc)
    ds.attach(scheduler_obj, hour=3, minute=15)
    scheduler_obj.add_job.assert_called_once()
    kwargs = scheduler_obj.add_job.call_args.kwargs
    assert kwargs["trigger"] == "cron"
    assert kwargs["hour"] == 3
    assert kwargs["minute"] == 15
    assert kwargs["id"] == "memory_decay"
    assert kwargs["replace_existing"] is True


def test_attach_default_2am():
    ds = DecayScheduler(MagicMock())
    s = MagicMock()
    ds.attach(s)
    assert s.add_job.call_args.kwargs["hour"] == 2
    assert s.add_job.call_args.kwargs["minute"] == 0


# ── run_decay_cycle ───────────────────────────────────────────────────────────


def _make_node(node_id: str, decay_score: float | None, days_old: float) -> dict:
    updated = datetime.now(UTC) - timedelta(days=days_old)
    return {
        "id": node_id,
        "decayScore": decay_score,
        "updatedAt": updated,
    }


@pytest.fixture
def graph_client():
    gc = MagicMock()
    gc.update_decay_scores = AsyncMock()
    gc.soft_delete_node = AsyncMock()
    return gc


def _stub_fetch(scheduler: DecayScheduler, nodes: list[dict]) -> None:
    """Replace _fetch_tracked_nodes with a coroutine returning the given nodes."""
    scheduler._fetch_tracked_nodes = AsyncMock(return_value=nodes)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_fresh_nodes_get_score_update(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("n1", 1.0, days_old=1)])
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 1
    assert stats["soft_deleted"] == 0
    graph_client.update_decay_scores.assert_awaited_once()
    new_scores = graph_client.update_decay_scores.await_args.args[0]
    # 1 day decay: score ≈ exp(-0.099) ≈ 0.906
    assert 0.85 < new_scores["n1"] < 0.95


@pytest.mark.asyncio
async def test_decayed_node_soft_deleted(graph_client):
    ds = DecayScheduler(graph_client)
    # Node old enough that score falls below threshold
    # threshold=0.05, current=1.0, days such that exp(-0.099*d) < 0.05
    # d > ln(20)/0.099 ≈ 30.3 days
    _stub_fetch(ds, [_make_node("n_old", 1.0, days_old=40)])
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 1
    assert stats["updated"] == 0
    graph_client.soft_delete_node.assert_awaited_once_with("n_old")
    graph_client.update_decay_scores.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_nodes_routed_correctly(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(
        ds,
        [
            _make_node("fresh", 1.0, days_old=1),
            _make_node("middle", 0.5, days_old=5),
            _make_node("dead", 0.5, days_old=50),
        ],
    )
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 2
    assert stats["soft_deleted"] == 1
    graph_client.soft_delete_node.assert_awaited_once_with("dead")


@pytest.mark.asyncio
async def test_node_without_id_skipped(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("", 1.0, days_old=1)])
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 0
    graph_client.update_decay_scores.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_without_updated_at_skipped(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [{"id": "n1", "decayScore": 1.0, "updatedAt": None}])
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 0


@pytest.mark.asyncio
async def test_missing_decay_score_uses_initial(graph_client):
    ds = DecayScheduler(graph_client)
    # decayScore=None → should default to 1.0
    _stub_fetch(ds, [_make_node("n1", None, days_old=1)])
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 1
    new_scores = graph_client.update_decay_scores.await_args.args[0]
    # Starting at 1.0, after 1 day → ~0.906
    assert 0.85 < new_scores["n1"] < 0.95


@pytest.mark.asyncio
async def test_fetch_failure_returns_empty_stats(graph_client):
    ds = DecayScheduler(graph_client)
    ds._fetch_tracked_nodes = AsyncMock(side_effect=RuntimeError("neo4j down"))  # type: ignore[method-assign]
    stats = await ds.run_decay_cycle()
    assert stats == {"updated": 0, "soft_deleted": 0, "errors": 0}
    graph_client.update_decay_scores.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_failure_increments_errors(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("n1", 1.0, days_old=1)])
    graph_client.update_decay_scores.side_effect = RuntimeError("write failed")
    stats = await ds.run_decay_cycle()
    assert stats["errors"] == 1
    assert stats["updated"] == 0  # not incremented on failure


@pytest.mark.asyncio
async def test_soft_delete_partial_failure_continues(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(
        ds,
        [
            _make_node("dead1", 0.5, days_old=50),
            _make_node("dead2", 0.5, days_old=50),
        ],
    )
    graph_client.soft_delete_node.side_effect = [RuntimeError("fail"), None]
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 1
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_empty_node_list_returns_zero_stats(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [])
    stats = await ds.run_decay_cycle()
    assert stats == {"updated": 0, "soft_deleted": 0, "errors": 0}


# ── Decay math sanity ─────────────────────────────────────────────────────────


def test_decay_rate_halves_in_seven_days():
    # ln(2)/7 ≈ 0.099 — verify the constant matches the documented intent
    assert abs(_DECAY_RATE - math.log(2) / 7) < 0.001


def test_delete_threshold_below_one():
    assert 0 < _DELETE_THRESHOLD < 1
