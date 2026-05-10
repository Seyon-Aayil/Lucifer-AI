"""Unit tests for master.core.auth.device_pairing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.core.auth.device_pairing import (
    PairingCodeStore,
    _generate_code,
    _is_valid_format,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _store(redis: MagicMock) -> PairingCodeStore:
    return PairingCodeStore(redis)


# ── Format / generator ───────────────────────────────────────────────────────


def test_generate_code_is_six_digits():
    for _ in range(50):
        c = _generate_code()
        assert len(c) == 6
        assert c.isdigit()


def test_generate_code_no_leading_zero():
    # Range 100000-999999 → no ambiguous leading-zero codes
    for _ in range(50):
        c = _generate_code()
        assert c[0] != "0"


@pytest.mark.parametrize(
    "code,ok",
    [
        ("123456", True),
        ("000000", True),  # technically valid format; generator avoids them
        ("12345", False),
        ("1234567", False),
        ("12a456", False),
        ("", False),
    ],
)
def test_is_valid_format(code, ok):
    assert _is_valid_format(code) is ok


# ── mint / verify round-trip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mint_writes_to_redis_with_ttl():
    redis = MagicMock()
    redis.set = AsyncMock()
    store = _store(redis)
    code = await store.mint(issued_for="laptop")
    redis.set.assert_awaited_once()
    args, kwargs = redis.set.await_args
    # First positional is the redis key, prefixed with the namespace
    assert args[0].endswith(code.code)
    assert args[1] == "laptop"
    assert kwargs.get("ex") == code.ttl_seconds


@pytest.mark.asyncio
async def test_verify_returns_payload_on_hit():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=b"laptop")
    store = _store(redis)
    result = await store.verify("123456")
    assert result == "laptop"


@pytest.mark.asyncio
async def test_verify_returns_none_on_miss():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value=None)
    store = _store(redis)
    assert await store.verify("123456") is None


@pytest.mark.asyncio
async def test_verify_handles_string_payload():
    redis = MagicMock()
    redis.eval = AsyncMock(return_value="phone")
    store = _store(redis)
    assert await store.verify("123456") == "phone"


@pytest.mark.asyncio
async def test_verify_rejects_malformed_code_without_redis_call():
    redis = MagicMock()
    redis.eval = AsyncMock()
    store = _store(redis)
    assert await store.verify("not-digits") is None
    redis.eval.assert_not_awaited()
    assert await store.verify("12345") is None
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_mint_defaults_issued_for_to_empty_string():
    redis = MagicMock()
    redis.set = AsyncMock()
    store = _store(redis)
    code = await store.mint()
    args, _ = redis.set.await_args
    assert args[1] == ""
    assert code.issued_for is None
