"""
master.sync.workers
====================
NATS JetStream consumer: applies sync deltas published by the gRPC servicer
to Neo4j and fans out `memory.delta.>` events for Librarian processing.

Subject consumed: `sync.edge.>` (published per device by SyncStream RPC).
Each message carries a JSON payload: {device_id, node_ids, edge_ids}.

The worker is a long-running asyncio task started by the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from master.core.logging import get_logger

log = get_logger(__name__)

_CONSUMER_NAME = "lucifer-sync-worker"
_SUBJECT = "sync.edge.>"
_STREAM = "lucifer-sync"


class SyncDeltaWorker:
    """
    Subscribes to NATS `sync.edge.>` and fans out memory.delta.> events
    so the Librarian can react to edge-originated graph writes.
    """

    def __init__(self, nats_js: Any, audit_logger: Any) -> None:
        self._js = nats_js
        self._audit = audit_logger
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sync_delta_worker")
        log.info("sync_delta_worker.started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("sync_delta_worker.stopped")

    async def _run(self) -> None:
        try:
            sub = await self._js.subscribe(_SUBJECT, durable=_CONSUMER_NAME)
            async for msg in sub.messages:
                await self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("sync_delta_worker.error", error=str(exc))

    async def _handle(self, msg: Any) -> None:
        try:
            data = json.loads(msg.data)
            device_id = data.get("device_id", "unknown")
            node_ids: list[str] = data.get("node_ids", [])
            edge_ids: list[str] = data.get("edge_ids", [])

            # Fan out memory.delta.> so Librarian can react (e.g. update embeddings)
            if node_ids:
                delta_payload = json.dumps(
                    {"source": "sync", "device_id": device_id, "node_ids": node_ids}
                ).encode()
                await self._js.publish(f"memory.delta.{device_id}", delta_payload)

            log.debug(
                "sync_delta_worker.processed",
                device_id=device_id,
                nodes=len(node_ids),
                edges=len(edge_ids),
            )
            await msg.ack()

        except Exception as exc:
            log.error("sync_delta_worker.handle_error", error=str(exc))
            await msg.nak()
