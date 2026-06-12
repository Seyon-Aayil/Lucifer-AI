"""
Integration tests for PairingCodeStore against a real Redis.

Run against the Docker `redis` service in CI:
    pytest master/tests/integration/ -m integration

Validates the single-use (Lua GETDEL) pairing-code round-trip that the unit
suite mocks out. Skips when Redis is unreachable (local run without `make up`).
"""

from __future__ import annotations

import os

import pytest

aioredis = pytest.importorskip("redis.asyncio")

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — unreachable means skip
        pytest.skip(f"Redis not reachable for integration tests: {exc}")
    try:
        yield client
    finally:
        await client.aclose()


async def test_mint_then_verify_returns_issued_for(redis_client) -> None:
    from master.core.auth.device_pairing import PairingCodeStore

    store = PairingCodeStore(redis_client)
    minted = await store.mint(issued_for="itest-macbook")
    assert len(minted.code) == 6
    assert minted.code.isdigit()
    assert await store.verify(minted.code) == "itest-macbook"


async def test_verify_is_single_use(redis_client) -> None:
    from master.core.auth.device_pairing import PairingCodeStore

    store = PairingCodeStore(redis_client)
    minted = await store.mint(issued_for="itest-single-use")

    assert await store.verify(minted.code) == "itest-single-use"
    # Second consume must miss — the Lua GETDEL deleted the key atomically.
    assert await store.verify(minted.code) is None


async def test_mint_without_hint_verifies_to_empty_string(redis_client) -> None:
    from master.core.auth.device_pairing import PairingCodeStore

    store = PairingCodeStore(redis_client)
    minted = await store.mint()
    assert await store.verify(minted.code) == ""


async def test_bad_format_codes_rejected(redis_client) -> None:
    from master.core.auth.device_pairing import PairingCodeStore

    store = PairingCodeStore(redis_client)
    assert await store.verify("12345") is None  # too short
    assert await store.verify("1234567") is None  # too long
    assert await store.verify("12a456") is None  # non-digit
