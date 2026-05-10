"""
master.llm.circuit_breaker
============================
Per-provider circuit breaker (half-open/open/closed state machine).
Trips after N consecutive failures; auto-recovers after a cooldown window.
Integrated into ProviderRegistry — failing providers are skipped in selection.
"""

from __future__ import annotations

import asyncio
import enum
import time

from master.core.logging import get_logger

log = get_logger(__name__)


class CircuitState(enum.StrEnum):
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Too many failures — reject immediately
    HALF_OPEN = "half_open"  # Cooldown elapsed — allow one probe request


class CircuitBreaker:
    """
    Circuit breaker for a single LLM provider.
    Thread-safe via asyncio.Lock.
    Args:
        provider_id: Identifier for log messages.
        failure_threshold: Consecutive failures before tripping.
        cooldown_seconds: Time to stay OPEN before probing.
        success_threshold: Successes in HALF_OPEN needed to close.
    """

    def __init__(
        self,
        provider_id: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        success_threshold: int = 2,
    ) -> None:
        self._provider_id = provider_id
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._success_threshold = success_threshold

        self._state: CircuitState = CircuitState.CLOSED
        self._failures: int = 0
        self._successes: int = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    async def can_proceed(self) -> bool:
        """
        Return True if the circuit allows a call to proceed.
        - CLOSED: always True.
        - OPEN: True only if cooldown has elapsed (moves to HALF_OPEN).
        - HALF_OPEN: True (probe allowed).
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._opened_at >= self._cooldown:
                    self._state = CircuitState.HALF_OPEN
                    self._successes = 0
                    log.info("circuit_breaker.half_open", provider=self._provider_id)
                    return True
                return False
            # HALF_OPEN: allow the probe
            return True

    async def record_success(self) -> None:
        """Record a successful call. Resets to CLOSED if threshold met."""
        async with self._lock:
            self._failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self._success_threshold:
                    self._state = CircuitState.CLOSED
                    log.info("circuit_breaker.closed", provider=self._provider_id)

    async def record_failure(self) -> None:
        """Record a failed call. Opens circuit if failure threshold exceeded."""
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold or self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                log.warning(
                    "circuit_breaker.open",
                    provider=self._provider_id,
                    failures=self._failures,
                )
