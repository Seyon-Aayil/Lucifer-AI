"""
master.core.auth.revocation
============================
Device and token revocation backed by Redis.
Revoked device IDs propagate within the TTL of the Redis entry (default 60s
cache + background refresh keeps this below the spec's 60s propagation window).
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)

# Redis key namespace
_NS = "lucifer:revoked"
_DEVICE_KEY = f"{_NS}:device"
_JTI_KEY = f"{_NS}:jti"


def _device_redis_key(device_id: str) -> str:
    return f"{_DEVICE_KEY}:{device_id}"


def _jti_redis_key(jti: str) -> str:
    return f"{_JTI_KEY}:{jti}"


class RevocationStore:
    """
    Redis-backed revocation store for devices and individual JTIs.
    Injected into middleware via FastAPI dependency injection.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._settings = get_settings()

    async def revoke_device(self, device_id: str, ttl_seconds: int = 86400 * 30) -> None:
        """
        Revoke all tokens for a device. Entries expire after ttl_seconds.
        The revocation propagates to all instances within the Redis read latency.
        """
        key = _device_redis_key(device_id)
        await self._redis.set(key, datetime.now(UTC).isoformat(), ex=ttl_seconds)
        log.info("auth.device.revoked", device_id=device_id)

    async def is_device_revoked(self, device_id: str) -> bool:
        """Return True if the device has been revoked."""
        return bool(await self._redis.exists(_device_redis_key(device_id)))

    async def revoke_jti(self, jti: str, ttl_seconds: int | None = None) -> None:
        """
        Revoke a single JWT by its JTI claim.
        TTL defaults to the access token TTL from settings.
        """
        ttl = ttl_seconds or self._settings.jwt_access_token_ttl_seconds
        await self._redis.set(_jti_redis_key(jti), "1", ex=ttl)
        log.debug("auth.jti.revoked", jti=jti)

    async def is_jti_revoked(self, jti: str) -> bool:
        """Return True if the specific token has been revoked."""
        return bool(await self._redis.exists(_jti_redis_key(jti)))
