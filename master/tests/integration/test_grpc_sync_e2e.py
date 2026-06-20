"""
End-to-end integration test for the gRPC LuciferSync server.

Exercises the wired server — DeviceAuthInterceptor (JWT + revocation) → servicer
→ ConflictResolver → real Neo4j / Postgres persistence → ack — against the Docker
services CI provisions. NATS has no CI container and is only reached via
`.publish(...)` in the SyncStream path, so `nats_js` is faked; everything else
(Postgres, Neo4j, Redis) is real.

    pytest master/tests/integration/ -m integration

Skips cleanly when any backing service is unreachable (local run without
`make up`).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

grpc = pytest.importorskip("grpc")
asyncpg = pytest.importorskip("asyncpg")
aioredis = pytest.importorskip("redis.asyncio")
neo4j = pytest.importorskip("neo4j")

pytestmark = pytest.mark.integration

_ID_PREFIX = "grpc-itest"


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "postgresql://lucifer:testpassword@localhost:5432/lucifer")
    return url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )


@pytest.fixture
async def grpc_env():
    """
    Stand up the full gRPC server over real Postgres/Neo4j/Redis (NATS + audit
    faked) and yield a namespace with the client stub plus the live handles the
    tests assert against. Skips if any service is unreachable.
    """
    from master.agents.librarian.graph_client import GraphClient
    from master.core.auth.revocation import RevocationStore
    from master.sync.auth import DeviceAuthInterceptor
    from master.sync.lucifer_sync_pb2_grpc import (
        LuciferSyncStub,
        add_LuciferSyncServicer_to_server,
    )
    from master.sync.server import LuciferSyncServicer

    # ── Real services (skip if any down) ──────────────────────────────────────
    try:
        pg_pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")

    redis_client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await redis_client.ping()
    except Exception as exc:  # noqa: BLE001
        await pg_pool.close()
        pytest.skip(f"Redis not reachable: {exc}")

    driver = neo4j.AsyncGraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "testpassword"),
        ),
    )
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        await redis_client.aclose()
        await pg_pool.close()
        await driver.close()
        pytest.skip(f"Neo4j not reachable: {exc}")

    graph_client = GraphClient(driver)
    revocation_store = RevocationStore(redis_client)
    nats_js = AsyncMock()
    audit_logger = AsyncMock()

    async def _wipe() -> None:
        async with driver.session() as s:
            await s.run(f"MATCH (n) WHERE n.id STARTS WITH '{_ID_PREFIX}' DETACH DELETE n")
        await pg_pool.execute(
            "DELETE FROM telemetry_events WHERE device_id LIKE $1", f"{_ID_PREFIX}%"
        )

    await _wipe()

    # ── Server: same wiring as runtime.start_grpc_server, port 0 captured ─────
    interceptor = DeviceAuthInterceptor(revocation_store)
    server = grpc.aio.server(interceptors=[interceptor])
    servicer = LuciferSyncServicer(
        graph_client=graph_client,
        db_pool=pg_pool,
        nats_js=nats_js,
        audit_logger=audit_logger,
        redis=redis_client,
    )
    add_LuciferSyncServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = LuciferSyncStub(channel)

    try:
        yield SimpleNamespace(
            stub=stub,
            graph_client=graph_client,
            pg_pool=pg_pool,
            revocation_store=revocation_store,
            nats_js=nats_js,
            redis=redis_client,
        )
    finally:
        await channel.close()
        await server.stop(grace=0)
        await _wipe()
        await redis_client.aclose()
        await driver.close()
        await pg_pool.close()


def _token(device_id: str) -> str:
    from master.core.auth.jwt import create_access_token

    return create_access_token(device_id)


def _md(device_id: str):
    return [("authorization", f"Bearer {_token(device_id)}")]


async def _one(msg):
    yield msg


async def test_syncstream_persists_node(grpc_env) -> None:
    from master.sync.lucifer_sync_pb2 import NodeDelta, SyncMessage

    device = f"{_ID_PREFIX}-dev-sync"
    node_id = f"{_ID_PREFIX}-node-1"
    now_ms = int(time.time() * 1000)

    msg = SyncMessage(
        device_id=device,
        sync_session_id="sess-1",
        vector_clock=now_ms,
        node_deltas=[
            NodeDelta(
                node_id=node_id,
                operation="upsert",
                node_type="Concept",
                payload=b'{"text":"hello"}',
                classification="standard",
                updated_at=now_ms,
                source_agent="itest",
            )
        ],
    )

    acks = [r async for r in grpc_env.stub.SyncStream(_one(msg), metadata=_md(device))]

    assert len(acks) == 1
    assert acks[0].device_id == "master"
    assert acks[0].sync_session_id == "sess-1"

    # Node landed in Neo4j.
    node = await grpc_env.graph_client.get_node(node_id)
    assert node["id"] == node_id
    assert node["text"] == "hello"

    # Accepted deltas were published to NATS for fan-out.
    grpc_env.nats_js.publish.assert_awaited()
    subjects = [call.args[0] for call in grpc_env.nats_js.publish.await_args_list]
    assert f"sync.edge.{device}" in subjects


async def test_get_hot_subgraph_returns_visible_node(grpc_env) -> None:
    from master.sync.lucifer_sync_pb2 import SubgraphRequest

    device = f"{_ID_PREFIX}-dev-hot"
    node_id = f"{_ID_PREFIX}-hot-node"
    now_ms = int(time.time() * 1000)

    # Seed a standard, recent node directly with updatedAtMs (the ACL Cypher
    # filters on updatedAtMs, which GraphClient.upsert_node does not set).
    async with grpc_env.graph_client._driver.session() as s:
        await s.run(
            """
            CREATE (n:Concept)
            SET n.id = $id, n.updatedAtMs = $ts, n.classification = 'standard',
                n.localOnly = false, n.deletedAt = null, n.sourceAgent = 'itest'
            """,
            id=node_id,
            ts=now_ms,
        )

    resp = await grpc_env.stub.GetHotSubgraph(
        SubgraphRequest(device_id=device, last_sync_at=now_ms - 3_600_000),
        metadata=_md(device),
    )

    ids = {n.node_id for n in resp.nodes}
    assert node_id in ids
    assert len(resp.manifest_hash) == 64
    assert resp.is_full_sync is False


async def test_push_telemetry_writes_rows(grpc_env) -> None:
    from master.sync.lucifer_sync_pb2 import TelemetryBatch, TelemetryEvent

    device = f"{_ID_PREFIX}-tel"
    now_us = int(time.time() * 1_000_000)

    batch = TelemetryBatch(
        device_id=device,
        events=[
            TelemetryEvent(
                event_type="rpc.call",
                timestamp=now_us,
                device_id=device,
                agent_id="coding",
                trace_id="t1",
                span_id="s1",
                metrics={"latency_ms": 12.5},
                metadata={"region": "us"},
                cost_usd=0.001,
                error=False,
            ),
            TelemetryEvent(
                event_type="rpc.error",
                timestamp=now_us,
                device_id=device,
                agent_id="coding",
                trace_id="t2",
                span_id="s2",
                error=True,
                error_code="E_TIMEOUT",
            ),
        ],
    )

    ack = await grpc_env.stub.PushTelemetry(batch, metadata=_md(device))
    assert ack.accepted_count == 2
    assert ack.rejected_count == 0

    count = await grpc_env.pg_pool.fetchval(
        "SELECT count(*) FROM telemetry_events WHERE device_id = $1", device
    )
    assert count == 2


async def test_get_pending_results_pops_buffered(grpc_env) -> None:
    import json as _json

    from master.sync.agent_worker import results_key
    from master.sync.lucifer_sync_pb2 import ResultRequest

    device = f"{_ID_PREFIX}-results"
    key = results_key(device)
    await grpc_env.redis.delete(key)
    await grpc_env.redis.lpush(
        key,
        _json.dumps(
            {"task_id": "t-1", "agent_id": "coding", "final_output": "done", "completed_at": 1}
        ),
    )

    resp = await grpc_env.stub.GetPendingResults(
        ResultRequest(device_id=device), metadata=_md(device)
    )
    assert len(resp.results) == 1
    assert resp.results[0].task_id == "t-1"
    assert resp.results[0].final_output == "done"

    # second pull is empty (list was cleared)
    resp2 = await grpc_env.stub.GetPendingResults(
        ResultRequest(device_id=device), metadata=_md(device)
    )
    assert len(resp2.results) == 0


async def test_auth_rejects_missing_token(grpc_env) -> None:
    from master.sync.lucifer_sync_pb2 import SubgraphRequest

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await grpc_env.stub.GetHotSubgraph(SubgraphRequest(device_id="x", last_sync_at=0))
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_auth_rejects_revoked_device(grpc_env) -> None:
    from master.sync.lucifer_sync_pb2 import SubgraphRequest

    device = f"{_ID_PREFIX}-revoked"
    await grpc_env.revocation_store.revoke_device(device, ttl_seconds=60)

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await grpc_env.stub.GetHotSubgraph(
            SubgraphRequest(device_id=device, last_sync_at=0), metadata=_md(device)
        )
    assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED


def _device_payload_hmac_key(device_id: str) -> bytes:
    from master.core.config import get_settings
    from master.core.crypto import payload_hmac_key

    return payload_hmac_key(get_settings().app_secret_key, device_id)


def _sign(msg, key: bytes) -> bytes:
    import hashlib
    import hmac

    from master.sync.lucifer_sync_pb2 import SyncMessage

    clone = SyncMessage()
    clone.CopyFrom(msg)
    clone.ClearField("payload_hmac")
    return hmac.new(key, clone.SerializeToString(), hashlib.sha256).digest()


async def test_syncstream_accepts_per_device_signed_message(grpc_env) -> None:
    """A SyncMessage signed with the device's derived key is accepted and the
    node persists — proving the master derives the same per-device key."""
    from master.sync.lucifer_sync_pb2 import NodeDelta, SyncMessage

    device = f"{_ID_PREFIX}-dev-signed"
    node_id = f"{_ID_PREFIX}-node-signed"
    now_ms = int(time.time() * 1000)

    msg = SyncMessage(
        device_id=device,
        sync_session_id="sess-signed",
        vector_clock=now_ms,
        node_deltas=[
            NodeDelta(
                node_id=node_id,
                operation="upsert",
                node_type="Concept",
                payload=b'{"text":"signed"}',
                classification="standard",
                updated_at=now_ms,
                source_agent="itest",
            )
        ],
    )
    msg.payload_hmac = _sign(msg, _device_payload_hmac_key(device))

    acks = [r async for r in grpc_env.stub.SyncStream(_one(msg), metadata=_md(device))]
    assert len(acks) == 1
    assert acks[0].device_id == "master"

    node = await grpc_env.graph_client.get_node(node_id)
    assert node["id"] == node_id
    assert node["text"] == "signed"


async def test_syncstream_rejects_bad_signature(grpc_env) -> None:
    """A SyncMessage with a present-but-wrong MAC is aborted with DATA_LOSS."""
    from master.sync.lucifer_sync_pb2 import NodeDelta, SyncMessage

    device = f"{_ID_PREFIX}-dev-badsig"
    now_ms = int(time.time() * 1000)

    msg = SyncMessage(
        device_id=device,
        sync_session_id="sess-badsig",
        vector_clock=now_ms,
        node_deltas=[
            NodeDelta(
                node_id=f"{_ID_PREFIX}-node-badsig",
                operation="upsert",
                node_type="Concept",
                payload=b'{"text":"x"}',
                classification="standard",
                updated_at=now_ms,
                source_agent="itest",
            )
        ],
    )
    msg.payload_hmac = b"\x00" * 32  # present but invalid

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        [r async for r in grpc_env.stub.SyncStream(_one(msg), metadata=_md(device))]
    assert exc.value.code() == grpc.StatusCode.DATA_LOSS
