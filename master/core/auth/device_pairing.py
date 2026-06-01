"""
master.core.auth.device_pairing
================================
6-digit one-shot device pairing code store.

Codes are minted by the master web admin (`POST /devices/admin/code`) and
exchanged by an edge device (`POST /devices/pair`) for an access JWT and a
freshly-minted client cert.  Codes:

- Are uniformly distributed across 100000–999999 (no leading zero confusion)
- Live in Redis under `lucifer:pairing:code:<code>` with a 60 s TTL
- Are single-use: the verify call deletes the key atomically via DEL+GET (Lua)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from redis.asyncio import Redis

from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)

_REDIS_PREFIX = "lucifer:pairing:code:"

# Atomic GET + DEL: returns the value if it existed, else nil.
_LUA_GETDEL = """
local v = redis.call('GET', KEYS[1])
if v then redis.call('DEL', KEYS[1]) end
return v
"""


@dataclass(frozen=True)
class PairingCode:
    """Result of minting a new pairing code."""

    code: str
    ttl_seconds: int
    issued_for: str | None  # optional human-readable hint (e.g. "macbook-pro")


class PairingCodeStore:
    """Redis-backed one-shot pairing code store."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._ttl = get_settings().pairing_code_ttl_seconds

    async def mint(self, issued_for: str | None = None) -> PairingCode:
        """Generate, store, and return a new 6-digit code."""
        code = _generate_code()
        await self._redis.set(
            f"{_REDIS_PREFIX}{code}",
            issued_for or "",
            ex=self._ttl,
        )
        log.info("device_pairing.code.minted", issued_for=issued_for or None)
        return PairingCode(code=code, ttl_seconds=self._ttl, issued_for=issued_for)

    async def verify(self, code: str) -> str | None:
        """
        Atomically consume a code. Returns the `issued_for` hint (possibly
        empty string) on success, or `None` if the code is unknown / expired /
        already-consumed.
        """
        if not _is_valid_format(code):
            return None
        # eval returns bytes or None
        raw = await self._redis.eval(
            _LUA_GETDEL,
            1,
            f"{_REDIS_PREFIX}{code}",
        )
        if raw is None:
            log.info("device_pairing.code.miss", code_prefix=code[:2])
            return None
        if isinstance(raw, bytes):
            return raw.decode()
        return str(raw)


def _generate_code() -> str:
    """Cryptographically uniform 6-digit code (no leading-zero ambiguity)."""
    return str(secrets.randbelow(900_000) + 100_000)


def _is_valid_format(code: str) -> bool:
    return len(code) == 6 and code.isdigit()
