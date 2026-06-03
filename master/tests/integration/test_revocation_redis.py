"""
Integration tests for RevocationStore against a real Redis.

Run against the Docker `redis` service in CI:
    pytest master/tests/integration/ -m integration

Validates the device / JTI / cert-serial revocation round-trips that the unit
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


async def test_device_revocation_roundtrip(redis_client) -> None:
    from master.core.auth.revocation import RevocationStore

    store = RevocationStore(redis_client)
    device = "itest-device-revoke"

    assert await store.is_device_revoked(device) is False
    await store.revoke_device(device, ttl_seconds=60)
    assert await store.is_device_revoked(device) is True


async def test_jti_revocation_roundtrip(redis_client) -> None:
    from master.core.auth.revocation import RevocationStore

    store = RevocationStore(redis_client)
    jti = "itest-jti-12345"

    assert await store.is_jti_revoked(jti) is False
    await store.revoke_jti(jti, ttl_seconds=60)
    assert await store.is_jti_revoked(jti) is True


async def test_cert_serial_revocation_and_listing(redis_client) -> None:
    from master.core.auth.revocation import RevocationStore

    store = RevocationStore(redis_client)
    serial = 0xDEADBEEF

    assert await store.is_cert_serial_revoked(serial) is False
    await store.revoke_cert_serial(serial)
    assert await store.is_cert_serial_revoked(serial) is True

    listed = await store.list_revoked_cert_serials()
    # stored canonical form is lowercase hex without the 0x prefix
    assert f"{serial:x}" in listed
