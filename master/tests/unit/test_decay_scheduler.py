"""Unit tests for master.agents.librarian.decay_scheduler."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from master.agents.librarian.decay_scheduler import (
    _DECAY_RATE,
    _DELETE_THRESHOLD,
    SOFT_DELETE_HORIZON_DAYS,
    DecayScheduler,
    _latest,
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


def _make_node(
    node_id: str,
    decay_score: float | None,
    days_old: float,
    read_days_ago: float | None = None,
) -> dict:
    """
    Build a fetched-node dict.

    Args:
        days_old: days since the node was last *written* (updatedAt).
        read_days_ago: days since the node was last *read* (lastAccessedAt).
            None means never read.
    """
    now = datetime.now(UTC)
    return {
        "id": node_id,
        "decayScore": decay_score,
        "updatedAt": now - timedelta(days=days_old),
        "lastAccessedAt": (None if read_days_ago is None else now - timedelta(days=read_days_ago)),
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


# ── _latest ───────────────────────────────────────────────────────────────────


def test_latest_all_none():
    assert _latest(None, None) is None


def test_latest_picks_most_recent():
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    assert _latest(older, newer) == newer
    assert _latest(newer, older) == newer


def test_latest_ignores_none():
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    assert _latest(None, dt) == dt
    assert _latest(dt, None) == dt


# ── Soft-delete horizon (ADR-011 regression guard) ────────────────────────────
#
# These tests exist because the previous implementation multiplied the
# already-decayed score by exp(-rate * days_since_write), compounding nightly to
# exp(-rate * N(N+1)/2) and killing nodes at ~8 days instead of the documented
# ~30. The old suite never caught it: every test ran a SINGLE cycle from a fresh
# score of 1.0, where the buggy and correct formulas agree. The horizon and the
# multi-night cases below are what actually pin the behaviour.


@pytest.mark.asyncio
async def test_node_just_under_horizon_survives(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("n29", 1.0, days_old=SOFT_DELETE_HORIZON_DAYS - 1)])
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 0, "a node written 29 days ago must survive"
    assert stats["updated"] == 1


@pytest.mark.asyncio
async def test_node_just_over_horizon_soft_deleted(graph_client):
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("n31", 1.0, days_old=SOFT_DELETE_HORIZON_DAYS + 1)])
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 1, "a node written 31 days ago must be soft-deleted"
    graph_client.soft_delete_node.assert_awaited_once_with("n31")


@pytest.mark.asyncio
async def test_repeated_cycles_do_not_compound(graph_client):
    """
    THE regression test for ADR-011.

    Simulate 10 consecutive nightly runs against a node written 8 days ago,
    feeding each run's output score back in as the buggy version did. The score
    must be identical every night — decay is a pure function of recency, not of
    the previous score — and the node must never be soft-deleted.
    """
    ds = DecayScheduler(graph_client)
    node = _make_node("n8", 1.0, days_old=8)
    scores = []

    for _ in range(10):
        graph_client.update_decay_scores.reset_mock()
        graph_client.soft_delete_node.reset_mock()
        _stub_fetch(ds, [dict(node)])
        stats = await ds.run_decay_cycle()

        assert stats["soft_deleted"] == 0, "8-day-old node must never be soft-deleted"
        emitted = graph_client.update_decay_scores.await_args.args[0]["n8"]
        scores.append(emitted)
        # Feed the result back in, exactly as the nightly job re-reads it.
        node["decayScore"] = emitted

    # Stable across all 10 runs. Tolerance is for wall-clock drift between
    # cycles (microseconds of real elapsed time), not for algorithmic decay.
    for i, score in enumerate(scores):
        assert score == pytest.approx(scores[0], rel=1e-6), (
            f"run {i} drifted from run 0 — decay is compounding: {scores}"
        )
    assert scores[0] == pytest.approx(math.exp(-_DECAY_RATE * 8), rel=1e-6)

    # Explicitly assert the old bug is gone. The previous implementation
    # (score *= exp(-rate * days_since_write)) would reach
    # exp(-rate * 8 * 10) ≈ 0.0004 by run 10 and soft-delete on run 2.
    buggy_run_10 = math.exp(-_DECAY_RATE * 8 * 10)
    assert scores[-1] > _DELETE_THRESHOLD > buggy_run_10


@pytest.mark.asyncio
async def test_read_keeps_node_alive_past_write_horizon(graph_client):
    """A node written 60 days ago but read yesterday must survive."""
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("hot", 0.01, days_old=60, read_days_ago=1)])
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 0
    score = graph_client.update_decay_scores.await_args.args[0]["hot"]
    assert score == pytest.approx(math.exp(-_DECAY_RATE * 1), rel=1e-6)


@pytest.mark.asyncio
async def test_stale_read_does_not_rescue_node(graph_client):
    """Read AND write both older than the horizon → still soft-deleted."""
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("cold", 1.0, days_old=90, read_days_ago=45)])
    stats = await ds.run_decay_cycle()
    assert stats["soft_deleted"] == 1


@pytest.mark.asyncio
async def test_node_with_only_last_accessed_is_scored(graph_client):
    """lastAccessedAt alone is enough — never-rewritten nodes still get scored."""
    ds = DecayScheduler(graph_client)
    _stub_fetch(
        ds,
        [{"id": "n1", "decayScore": 0.5, "updatedAt": None, "lastAccessedAt": datetime.now(UTC)}],
    )
    stats = await ds.run_decay_cycle()
    assert stats["updated"] == 1
    assert graph_client.update_decay_scores.await_args.args[0]["n1"] == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_score_ignores_previous_value(graph_client):
    """Two nodes, same recency, wildly different stored scores → same new score."""
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("a", 1.0, days_old=3), _make_node("b", 0.06, days_old=3)])
    await ds.run_decay_cycle()
    emitted = graph_client.update_decay_scores.await_args.args[0]
    assert emitted["a"] == pytest.approx(emitted["b"], rel=1e-9)


@pytest.mark.asyncio
async def test_future_timestamp_clamps_to_full_score(graph_client):
    """Clock skew must not produce a score above 1.0."""
    ds = DecayScheduler(graph_client)
    _stub_fetch(ds, [_make_node("skew", 1.0, days_old=-5)])
    await ds.run_decay_cycle()
    assert graph_client.update_decay_scores.await_args.args[0]["skew"] == pytest.approx(1.0)


# ── Decay math sanity ─────────────────────────────────────────────────────────


def test_decay_rate_halves_in_seven_days():
    # ln(2)/7 ≈ 0.099 — verify the constant matches the documented intent
    assert abs(_DECAY_RATE - math.log(2) / 7) < 0.001


def test_delete_threshold_below_one():
    assert 0 < _DELETE_THRESHOLD < 1


def test_soft_delete_horizon_is_30_days():
    """
    The documented contract: ~30 days of no read and no write before deletion.
    Derived from rate + threshold, so this test fails if either drifts.
    """
    assert 30.0 < SOFT_DELETE_HORIZON_DAYS < 31.0
