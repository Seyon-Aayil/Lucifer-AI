"""Unit tests for cert revocation set in master.core.auth.revocation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.core.auth.revocation import RevocationStore, _format_serial


def _redis() -> MagicMock:
    r = MagicMock()
    r.sadd = AsyncMock()
    r.sismember = AsyncMock(return_value=False)
    r.smembers = AsyncMock(return_value=set())
    return r


def test_format_serial_lowercase_no_prefix():
    assert _format_serial(0xABCDEF) == "abcdef"
    assert _format_serial(255) == "ff"
    assert _format_serial(0) == "0"


@pytest.mark.asyncio
async def test_revoke_cert_serial_calls_sadd():
    redis = _redis()
    store = RevocationStore(redis)
    await store.revoke_cert_serial(0xCAFE)
    redis.sadd.assert_awaited_once()
    args = redis.sadd.await_args.args
    assert args[1] == "cafe"


@pytest.mark.asyncio
async def test_is_cert_revoked_true_when_member():
    redis = _redis()
    redis.sismember = AsyncMock(return_value=True)
    store = RevocationStore(redis)
    assert await store.is_cert_serial_revoked(0xCAFE) is True


@pytest.mark.asyncio
async def test_is_cert_revoked_false_when_missing():
    redis = _redis()
    store = RevocationStore(redis)
    assert await store.is_cert_serial_revoked(0xCAFE) is False


@pytest.mark.asyncio
async def test_list_revoked_handles_bytes_and_strings():
    redis = _redis()
    redis.smembers = AsyncMock(return_value={b"cafe", "deadbeef"})
    store = RevocationStore(redis)
    result = await store.list_revoked_cert_serials()
    assert set(result) == {"cafe", "deadbeef"}
