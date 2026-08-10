"""
Unit tests for the per-provider CircuitBreaker state machine.

Covers the closed → open → half-open → closed lifecycle, the failure/success
counters, and the cooldown gate — all deterministic by pinning
``time.monotonic`` so no real sleeping is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from master.llm.circuit_breaker import CircuitBreaker, CircuitState


def _breaker(**kwargs: object) -> CircuitBreaker:
    defaults: dict[str, object] = {
        "provider_id": "test-provider",
        "failure_threshold": 3,
        "cooldown_seconds": 60.0,
        "success_threshold": 2,
    }
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_starts_closed_and_allows_calls() -> None:
    cb = _breaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.is_open is False
    assert await cb.can_proceed() is True


@pytest.mark.asyncio
async def test_opens_after_failure_threshold() -> None:
    cb = _breaker(failure_threshold=3)
    await cb.record_failure()
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # not yet at threshold
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.is_open is True


@pytest.mark.asyncio
async def test_open_rejects_until_cooldown_elapses() -> None:
    cb = _breaker(failure_threshold=1, cooldown_seconds=30.0)
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=1000.0):
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # Still inside the cooldown window → rejected, stays OPEN.
        assert await cb.can_proceed() is False
        assert cb.state == CircuitState.OPEN

    # Cooldown elapsed → next probe is allowed and flips to HALF_OPEN.
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=1031.0):
        assert await cb.can_proceed() is True
        assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_closes_after_success_threshold() -> None:
    cb = _breaker(failure_threshold=1, cooldown_seconds=10.0, success_threshold=2)
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=0.0):
        await cb.record_failure()
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=20.0):
        assert await cb.can_proceed() is True  # → HALF_OPEN

    await cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN  # one success, need two
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_half_open_reopens_on_single_failure() -> None:
    cb = _breaker(failure_threshold=5, cooldown_seconds=10.0)
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=0.0):
        # Force OPEN directly through the failure threshold.
        for _ in range(5):
            await cb.record_failure()
        assert cb.state == CircuitState.OPEN
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=20.0):
        assert await cb.can_proceed() is True  # → HALF_OPEN
    # A single failure in HALF_OPEN trips straight back to OPEN.
    with patch("master.llm.circuit_breaker.time.monotonic", return_value=21.0):
        await cb.record_failure()
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_success_resets_failure_counter_while_closed() -> None:
    cb = _breaker(failure_threshold=3)
    await cb.record_failure()
    await cb.record_failure()
    await cb.record_success()  # resets the streak
    await cb.record_failure()
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # only 2 consecutive since reset
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_circuit_state_values() -> None:
    assert CircuitState.CLOSED == "closed"
    assert CircuitState.OPEN == "open"
    assert CircuitState.HALF_OPEN == "half_open"
