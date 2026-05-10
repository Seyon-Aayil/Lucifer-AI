"""
master.core.auth.rate_limit
============================
Redis-backed sliding-window rate limiter for sensitive auth endpoints.

The pairing endpoint (`POST /devices/pair`) is the worst offender: a
brute-forcer racing the 60-second code TTL has ~900 000 candidate codes,
which is well within reach of a single network. We cap each source IP at
5 attempts per 60 s — three orders of magnitude tighter than what the
network can deliver.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from master.core.logging import get_logger

log = get_logger(__name__)

_NS = "lucifer:ratelimit"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """
    Per-key fixed-window counter. Tradeoff vs. sliding-log: cheap (one
    INCR + one EXPIRE), one window of bursting on rollover, no per-event
    storage. Good enough for credential-handling endpoints with low expected
    legitimate traffic.
    """

    def __init__(self, redis: Redis, *, limit: int, window_seconds: int, namespace: str) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._redis = redis
        self._limit = limit
        self._window = window_seconds
        self._namespace = namespace

    async def check(self, key: str) -> RateLimitDecision:
        """
        Atomically increment the counter for `key` and decide whether the
        caller may proceed. Sets the TTL on first increment so the window
        ages out without needing a sweeper.
        """
        full_key = f"{_NS}:{self._namespace}:{key}"

        # INCR returns the new count. PIPELINE the EXPIRE so they hit Redis
        # in one round-trip.
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.incr(full_key)
            pipe.expire(full_key, self._window, nx=True)  # only set TTL on the first hit
            results = await pipe.execute()

        count = int(results[0])
        if count > self._limit:
            ttl = int(await self._redis.ttl(full_key))
            log.warning(
                "ratelimit.blocked",
                namespace=self._namespace,
                key=key,
                count=count,
                window=self._window,
            )
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                retry_after_seconds=max(1, ttl if ttl > 0 else self._window),
            )
        return RateLimitDecision(
            allowed=True,
            remaining=max(0, self._limit - count),
            retry_after_seconds=0,
        )
