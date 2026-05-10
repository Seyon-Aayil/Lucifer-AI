"""
master.sync.telemetry_sink
===========================
Ingest TelemetryBatch payloads (from edge devices via PushTelemetry RPC)
into the TimescaleDB `telemetry_events` hypertable.

Writes are batched per RPC call. Failures are recorded in the returned
counters; this RPC is fire-and-forget at-least-once, so partial failures
are acceptable and surfaced via PushAck.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from master.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class IngestResult:
    accepted: int
    rejected: int
    message: str = ""


class TelemetrySink:
    """
    Persists TelemetryEvent rows to TimescaleDB. Stateless aside from the
    asyncpg pool reference.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._pool = db_pool

    async def ingest(self, batch: Any) -> IngestResult:
        """
        Insert all events in `batch` into the hypertable.

        Args:
            batch: a `lucifer_sync_pb2.TelemetryBatch` instance.

        Returns:
            IngestResult with accepted/rejected counts.
        """
        events = list(getattr(batch, "events", []) or [])
        if not events:
            return IngestResult(accepted=0, rejected=0)

        rows = [self._row_for(batch.device_id, e) for e in events]

        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO telemetry_events
                        (timestamp, device_id, agent_id, event_type, trace_id, span_id,
                         metrics, metadata, cost_usd, error, error_code)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11)
                    """,
                    rows,
                )
            log.debug("telemetry_sink.ingested", count=len(rows), device=batch.device_id)
            return IngestResult(accepted=len(rows), rejected=0)
        except Exception as exc:
            log.error("telemetry_sink.failed", error=str(exc), device=batch.device_id)
            return IngestResult(accepted=0, rejected=len(rows), message=str(exc))

    @staticmethod
    def _row_for(device_id: str, event: Any) -> tuple[Any, ...]:
        # event.timestamp is Unix MICROSECONDS per the proto.
        from datetime import UTC, datetime

        ts = datetime.fromtimestamp(event.timestamp / 1_000_000, tz=UTC)
        return (
            ts,
            event.device_id or device_id,
            event.agent_id or None,
            event.event_type,
            event.trace_id or "",  # NOT NULL in schema
            event.span_id or "",  # NOT NULL in schema
            json.dumps(dict(event.metrics)),
            json.dumps(dict(event.metadata)),
            float(event.cost_usd) if event.cost_usd else None,
            bool(event.error),
            event.error_code or None,
        )
