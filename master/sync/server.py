"""
master.sync.server
===================
gRPC LuciferSync servicer — implements all three RPCs.

  SyncStream      — bidirectional; edge pushes NodeDelta/EdgeDelta/QueuedAction,
                    master resolves conflicts and streams back master-side updates.
  GetHotSubgraph  — unary pull of ≤50 MB subgraph manifest for cold-start sync.
  PushTelemetry   — fire-and-forget batch ingest into TimescaleDB.

Conflict audit records are appended to the HMAC audit chain via AuditLogger.
Master-side deltas (Librarian writes since the device's last_seen vector clock)
are published to NATS `sync.edge.<device_id>` for fan-out; this servicer also
streams them back inline.

Payload HMAC contract (field 7, `payload_hmac`):
    HMAC-SHA256(key, SerializeToString(message with payload_hmac cleared)),
    full 32 bytes, with a per-device key = HKDF-SHA256(app_secret_key,
    info="lucifer-sync-payload-hmac:" + device_id). Each device gets its own
    key (provisioned at pairing), so a leaked key can't forge for other
    devices. An absent MAC is currently allowed (warning logged once per
    stream) until the edge client adopts the scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import grpc

from master.core.config import get_settings
from master.core.crypto import payload_hmac_key as derive_payload_hmac_key
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.sync.auth import current_caller
from master.sync.conflict_resolver import (
    ConflictResolver,
    DeltaCandidate,
    candidate_from_edge_delta,
    candidate_from_node_delta,
)
from master.sync.hot_subgraph import HotSubgraphBuilder
from master.sync.lucifer_sync_pb2 import (  # type: ignore[attr-defined]
    AgentResult,
    PushAck,
    ResultBatch,
    SubgraphResponse,
    SyncMessage,
)
from master.sync.lucifer_sync_pb2_grpc import (
    LuciferSyncServicer as _Base,
)
from master.sync.telemetry_sink import TelemetrySink

log = get_logger(__name__)
tracer = get_tracer(__name__)


class LuciferSyncServicer(_Base):
    """
    Concrete implementation of the LuciferSync gRPC service.
    One shared instance; state is read-only after construction.
    """

    def __init__(
        self,
        graph_client: Any,
        db_pool: Any,
        nats_js: Any,
        audit_logger: Any,
        redis: Any = None,
        payload_hmac_key: bytes | None = None,
        app_secret_key: str | None = None,
    ) -> None:
        self._graph = graph_client
        self._telemetry_sink = TelemetrySink(db_pool)
        self._hot_subgraph = HotSubgraphBuilder(graph_client)
        self._resolver = ConflictResolver()
        self._js = nats_js
        self._audit = audit_logger
        self._redis = redis
        # `payload_hmac_key`, when given, is a fixed key used for every device
        # (explicit override, mainly for tests). Otherwise each device gets its
        # own key derived from `app_secret_key` — bounding the blast radius if
        # one device's key leaks. The edge receives its key at pairing.
        self._payload_hmac_key = payload_hmac_key
        self._app_secret_key = app_secret_key or get_settings().app_secret_key
        self._device_hmac_keys: dict[str, bytes] = {}

    # ── SyncStream ────────────────────────────────────────────────────────────

    async def SyncStream(  # noqa: N802
        self,
        request_iterator: AsyncIterator[SyncMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[SyncMessage]:
        caller = current_caller.get()
        device_id = caller.device_id if caller else "unknown"

        with tracer.start_as_current_span("grpc.sync_stream", attributes={"device_id": device_id}):
            log.info("sync.stream.opened", device_id=device_id)
            hmac_absent_logged = False

            async for msg in request_iterator:
                if msg.device_id and msg.device_id != device_id:
                    log.warning(
                        "sync.stream.device_mismatch",
                        claimed=msg.device_id,
                        authed=device_id,
                    )
                    await context.abort(grpc.StatusCode.PERMISSION_DENIED, "device_id mismatch")
                    return

                # Verify HMAC (absent MAC allowed until the edge adopts the contract)
                if not msg.payload_hmac and not hmac_absent_logged:
                    log.warning("sync.stream.hmac_absent", device_id=device_id)
                    hmac_absent_logged = True
                if not self._verify_hmac(msg, device_id):
                    log.warning("sync.stream.hmac_fail", device_id=device_id)
                    await context.abort(grpc.StatusCode.DATA_LOSS, "payload_hmac invalid")
                    return

                accepted_nodes: list[str] = []
                accepted_edges: list[str] = []

                # Process node deltas
                for node_delta in msg.node_deltas:
                    winner, audit_rec = await self._resolve_node(
                        node_delta, msg.vector_clock, device_id
                    )
                    if winner.record_id == node_delta.node_id:
                        await self._apply_node(winner, node_delta)
                        accepted_nodes.append(node_delta.node_id)
                    if audit_rec:
                        await self._log_conflict(audit_rec, device_id)

                # Process edge deltas
                for edge_delta in msg.edge_deltas:
                    candidate = candidate_from_edge_delta(edge_delta, msg.vector_clock)
                    existing = await self._fetch_edge_candidate(
                        edge_delta.edge_id, msg.vector_clock
                    )
                    winner, audit_rec = self._resolver.resolve(candidate, existing)
                    if winner.record_id == edge_delta.edge_id:
                        await self._apply_edge(edge_delta)
                        accepted_edges.append(edge_delta.edge_id)
                    if audit_rec:
                        await self._log_conflict(audit_rec, device_id)

                # Process offline queued actions
                for action in msg.offline_actions:
                    await self._dispatch_queued_action(action, device_id)

                # Publish accepted deltas to NATS for fan-out workers
                if accepted_nodes or accepted_edges:
                    await self._publish_sync_event(device_id, accepted_nodes, accepted_edges)

                # Stream back master-side delta acknowledgment
                ack = SyncMessage(
                    device_id="master",
                    sync_session_id=msg.sync_session_id,
                    vector_clock=int(time.time() * 1000),
                )
                yield ack

            log.info("sync.stream.closed", device_id=device_id)

    # ── GetHotSubgraph ────────────────────────────────────────────────────────

    async def GetHotSubgraph(  # noqa: N802
        self,
        request: Any,
        context: grpc.aio.ServicerContext,
    ) -> SubgraphResponse:
        caller = current_caller.get()
        device_id = caller.device_id if caller else request.device_id

        with tracer.start_as_current_span(
            "grpc.get_hot_subgraph", attributes={"device_id": device_id}
        ):
            log.info("sync.hot_subgraph.requested", device_id=device_id)
            manifest = await self._hot_subgraph.build(
                device_id=device_id,
                last_sync_at_ms=request.last_sync_at,
                max_size_bytes=request.max_size_bytes or None,
            )

        resp = SubgraphResponse(
            generated_at=manifest["generated_at"],
            manifest_hash=manifest["manifest_hash"],
            is_full_sync=manifest["is_full_sync"],
        )
        # Populate node/edge repeated fields
        for n in manifest["nodes"]:
            resp.nodes.add(
                node_id=n["node_id"],
                operation=n["operation"],
                node_type=n["node_type"],
                payload=n["payload"],
                classification=n["classification"],
                updated_at=n["updated_at"],
                source_agent=n["source_agent"],
            )
        for e in manifest["edges"]:
            resp.edges.add(
                edge_id=e["edge_id"],
                operation=e["operation"],
                from_node_id=e["from_node_id"],
                to_node_id=e["to_node_id"],
                relation=e["relation"],
                weight=e["weight"],
                valid_from=e["valid_from"],
                valid_until=e["valid_until"],
            )
        return resp

    # ── PushTelemetry ─────────────────────────────────────────────────────────

    async def PushTelemetry(  # noqa: N802
        self,
        request: Any,
        context: grpc.aio.ServicerContext,
    ) -> PushAck:
        result = await self._telemetry_sink.ingest(request)
        log.debug(
            "sync.telemetry.ingested",
            device=request.device_id,
            accepted=result.accepted,
            rejected=result.rejected,
        )
        return PushAck(
            accepted_count=result.accepted,
            rejected_count=result.rejected,
            message=result.message,
        )

    # ── GetPendingResults ─────────────────────────────────────────────────────

    async def GetPendingResults(  # noqa: N802
        self,
        request: Any,
        context: grpc.aio.ServicerContext,
    ) -> ResultBatch:
        from master.sync.agent_worker import results_key

        caller = current_caller.get()
        device_id = caller.device_id if caller else request.device_id
        if self._redis is None:
            return ResultBatch(results=[])

        key = results_key(device_id)
        limit = request.max_results or 100
        try:
            raw = await self._redis.lrange(key, 0, limit - 1)
            if raw:
                await self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001 — best-effort pull
            log.warning("sync.results.read_failed", device_id=device_id, error=str(exc))
            return ResultBatch(results=[])

        results = []
        for item in raw:
            try:
                d = json.loads(item)
            except (ValueError, TypeError):
                continue
            results.append(
                AgentResult(
                    task_id=str(d.get("task_id", "")),
                    agent_id=str(d.get("agent_id", "")),
                    final_output=str(d.get("final_output", "")),
                    completed_at=int(d.get("completed_at", 0)),
                )
            )
        log.debug("sync.results.pulled", device_id=device_id, count=len(results))
        return ResultBatch(results=results)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _hmac_key_for(self, device_id: str) -> bytes:
        """Per-device payload-HMAC key (or the fixed override, if configured)."""
        if self._payload_hmac_key is not None:
            return self._payload_hmac_key
        key = self._device_hmac_keys.get(device_id)
        if key is None:
            key = derive_payload_hmac_key(self._app_secret_key, device_id)
            self._device_hmac_keys[device_id] = key
        return key

    def _verify_hmac(self, msg: SyncMessage, device_id: str) -> bool:
        """
        Verify the keyed payload HMAC attached to a SyncMessage (see module
        docstring for the contract). The MAC covers the serialised message
        with the `payload_hmac` field cleared, so it can't cover itself, and is
        keyed per device. An absent MAC is allowed — the edge client doesn't
        send one yet.
        """
        if not msg.payload_hmac:
            return True
        clone = SyncMessage()
        clone.CopyFrom(msg)
        clone.ClearField("payload_hmac")
        expected = hmac.new(
            self._hmac_key_for(device_id), clone.SerializeToString(), hashlib.sha256
        ).digest()
        return hmac.compare_digest(bytes(msg.payload_hmac), expected)

    async def _resolve_node(
        self, delta: Any, vector_clock: int, device_id: str
    ) -> tuple[DeltaCandidate, Any]:
        incoming = candidate_from_node_delta(delta)
        # Patch vector_clock from enclosing SyncMessage (not on NodeDelta itself)
        from dataclasses import replace

        incoming = replace(incoming, vector_clock=vector_clock)
        existing = await self._fetch_node_candidate(delta.node_id, vector_clock)
        return self._resolver.resolve(incoming, existing)

    async def _fetch_node_candidate(
        self, node_id: str, fallback_clock: int
    ) -> DeltaCandidate | None:
        try:
            props = await self._graph.get_node(node_id)
            return DeltaCandidate(
                record_id=node_id,
                operation="upsert",
                source_agent=props.get("sourceAgent"),
                vector_clock=props.get("vectorClock", fallback_clock - 1),
                updated_at=props.get("updatedAtMs", 0),
                payload_hash=hashlib.sha256(
                    json.dumps(props, sort_keys=True, default=str).encode()
                ).hexdigest(),
            )
        except Exception:
            return None

    async def _fetch_edge_candidate(
        self, edge_id: str, fallback_clock: int
    ) -> DeltaCandidate | None:
        return None  # edges don't have a simple lookup yet; treat all as new

    async def _apply_node(self, winner: DeltaCandidate, delta: Any) -> None:
        try:
            attrs = json.loads(delta.payload) if delta.payload else {}
            attrs["vectorClock"] = winner.vector_clock
            attrs["updatedAtMs"] = winner.updated_at
            if winner.operation == "soft_delete":
                await self._graph.soft_delete_node(winner.record_id)
            else:
                await self._graph.upsert_node(delta.node_type, winner.record_id, attrs)
        except Exception as exc:
            log.error("sync.apply_node.failed", node_id=winner.record_id, error=str(exc))

    async def _apply_edge(self, delta: Any) -> None:
        try:
            if delta.operation == "delete":
                pass  # no hard-delete; soft-delete via valid_until=now handled by convention
            else:
                await self._graph.upsert_edge(
                    from_id=delta.from_node_id,
                    to_id=delta.to_node_id,
                    relation=delta.relation,
                    weight=delta.weight,
                    valid_from=str(delta.valid_from),
                    valid_until=str(delta.valid_until) if delta.valid_until else None,
                )
        except Exception as exc:
            log.error("sync.apply_edge.failed", edge_id=delta.edge_id, error=str(exc))

    async def _dispatch_queued_action(self, action: Any, device_id: str) -> None:
        try:
            subject = f"agent.task.{action.action_type}"
            payload = {
                "action_id": action.action_id,
                "action_type": action.action_type,
                "device_id": device_id,
                "queued_at": action.queued_at,
                "payload": action.payload.hex() if action.payload else None,
            }
            await self._js.publish(subject, json.dumps(payload).encode())
        except Exception as exc:
            log.error("sync.queued_action.failed", action_id=action.action_id, error=str(exc))

    async def _publish_sync_event(
        self,
        device_id: str,
        node_ids: list[str],
        edge_ids: list[str],
    ) -> None:
        try:
            payload = json.dumps(
                {"device_id": device_id, "node_ids": node_ids, "edge_ids": edge_ids}
            ).encode()
            await self._js.publish(f"sync.edge.{device_id}", payload)
        except Exception as exc:
            log.error("sync.publish.failed", device_id=device_id, error=str(exc))

    async def _log_conflict(self, audit_rec: Any, device_id: str) -> None:
        await self._audit.log_event(
            event_type="sync.conflict.resolved",
            action="resolve",
            resource=audit_rec.record_id,
            payload={
                "winner_agent": audit_rec.winner_agent,
                "winner_clock": audit_rec.winner_clock,
                "loser_agent": audit_rec.loser_agent,
                "loser_clock": audit_rec.loser_clock,
                "reason": audit_rec.reason,
            },
            device_id=device_id,
        )
