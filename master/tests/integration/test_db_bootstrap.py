"""
Integration tests for the Postgres/TimescaleDB bootstrap.

Run against the real Docker services in CI (the migration is applied first):
    pytest master/tests/integration/ -m integration

These validate the migration that the unit suite cannot reach:
- the pgvector / timescaledb / uuid-ossp extensions are actually installed
  (regression guard for the `pgvector` -> `vector` extension-name fix),
- `telemetry_events` is a hypertable with compression (columnstore) enabled
  (regression guard for the "enable compression before add_compression_policy"
  fix required by TimescaleDB 2.18+),
- TelemetrySink.ingest writes rows that come back out of the hypertable.

If no database is reachable (e.g. a local run without `make up`), the pool
fixture skips the whole module rather than failing collection.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.integration


def _dsn() -> str:
    """asyncpg wants a plain libpq DSN; strip any SQLAlchemy `+driver` suffix."""
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://lucifer:testpassword@localhost:5432/lucifer",
    )
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


@pytest.fixture
async def pg_pool():
    """asyncpg pool; skips the test if the DB is unreachable (e.g. no `make up`)."""
    try:
        pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001 — any connect failure means "skip"
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


async def test_required_extensions_installed(pg_pool) -> None:
    rows = await pg_pool.fetch("SELECT extname FROM pg_extension")
    names = {r["extname"] for r in rows}
    # pgvector registers under the name "vector"; "pgvector" is NOT valid.
    assert "vector" in names, f"pgvector (vector) missing; have {names}"
    assert "timescaledb" in names, f"timescaledb missing; have {names}"
    assert "uuid-ossp" in names, f"uuid-ossp missing; have {names}"


async def test_telemetry_events_is_hypertable(pg_pool) -> None:
    count = await pg_pool.fetchval(
        """
        SELECT count(*) FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'telemetry_events'
        """
    )
    assert count == 1, "telemetry_events is not a TimescaleDB hypertable"


async def test_telemetry_compression_enabled(pg_pool) -> None:
    # Regression guard: add_compression_policy needs compression enabled first
    # on TimescaleDB 2.18+. compression_enabled must be true.
    enabled = await pg_pool.fetchval(
        """
        SELECT compression_enabled FROM timescaledb_information.hypertables
        WHERE hypertable_name = 'telemetry_events'
        """
    )
    assert enabled is True, "columnstore/compression not enabled on telemetry_events"


def _event(**over):
    base = dict(
        timestamp=1_700_000_000_000_000,  # Unix MICROSECONDS
        device_id="itest-device",
        agent_id="coding",
        event_type="rpc.call",
        trace_id="itest-trace",
        span_id="itest-span",
        metrics={"latency_ms": 12.5},
        metadata={"region": "us"},
        cost_usd=0.0012,
        error=False,
        error_code="",
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_telemetry_sink_roundtrip(pg_pool) -> None:
    from master.sync.telemetry_sink import TelemetrySink

    device = "itest-roundtrip"
    batch = SimpleNamespace(
        device_id=device,
        events=[
            _event(device_id=device, event_type="rpc.call"),
            _event(device_id=device, event_type="rpc.error", error=True, error_code="E_TIMEOUT"),
        ],
    )

    # Clean any prior rows for this device so the test is idempotent.
    await pg_pool.execute("DELETE FROM telemetry_events WHERE device_id = $1", device)

    result = await TelemetrySink(pg_pool).ingest(batch)
    assert result.accepted == 2, result.message
    assert result.rejected == 0

    rows = await pg_pool.fetch(
        "SELECT event_type, error, error_code, agent_id, metrics "
        "FROM telemetry_events WHERE device_id = $1 ORDER BY event_type",
        device,
    )
    assert len(rows) == 2
    by_type = {r["event_type"]: r for r in rows}
    assert by_type["rpc.call"]["error"] is False
    assert by_type["rpc.error"]["error"] is True
    assert by_type["rpc.error"]["error_code"] == "E_TIMEOUT"
    assert by_type["rpc.call"]["agent_id"] == "coding"


async def test_telemetry_sink_empty_batch_is_noop(pg_pool) -> None:
    from master.sync.telemetry_sink import TelemetrySink

    result = await TelemetrySink(pg_pool).ingest(SimpleNamespace(device_id="x", events=[]))
    assert result.accepted == 0
    assert result.rejected == 0
