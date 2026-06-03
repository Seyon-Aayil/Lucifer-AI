"""
Integration tests for HotSubgraphBuilder against a real Neo4j.

Run against the Docker `neo4j` service in CI:
    pytest master/tests/integration/ -m integration

Validates the ACL-filtered Cypher pull that the unit suite can only exercise on
the pure size-cap / hashing helpers:
- secret / localOnly / soft-deleted nodes are excluded from the manifest,
- the `updatedAtMs` cutoff excludes stale nodes,
- a standard, recent node is included with a 64-hex manifest hash.

Skips when Neo4j is unreachable (local run without `make up`).
"""

from __future__ import annotations

import os
import time

import pytest

neo4j = pytest.importorskip("neo4j")

pytestmark = pytest.mark.integration

_MARK = "hotsub-itest"  # marker prop so the fixture can clean up only its nodes


@pytest.fixture
async def graph_client():
    from master.agents.librarian.graph_client import GraphClient

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "testpassword")

    driver = neo4j.AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        await driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 — unreachable means skip
        await driver.close()
        pytest.skip(f"Neo4j not reachable for integration tests: {exc}")

    async def _wipe() -> None:
        async with driver.session() as s:
            await s.run("MATCH (n {itest: $m}) DETACH DELETE n", m=_MARK)

    await _wipe()
    try:
        yield GraphClient(driver)
    finally:
        await _wipe()
        await driver.close()


async def _seed(driver, node_id: str, now_ms: int, **props) -> None:
    async with driver.session() as s:
        await s.run(
            """
            CREATE (n:ITestNode)
            SET n.id = $id, n.itest = $mark, n.updatedAtMs = $ts,
                n.classification = $classification, n.localOnly = $local_only,
                n.deletedAt = $deleted_at, n.sourceAgent = 'itest'
            """,
            id=node_id,
            mark=_MARK,
            ts=props.get("updated_ms", now_ms),
            classification=props.get("classification", "standard"),
            local_only=props.get("local_only", False),
            deleted_at=props.get("deleted_at"),
        )


async def test_acl_and_cutoff_filtering(graph_client) -> None:
    from master.sync.hot_subgraph import HotSubgraphBuilder

    driver = graph_client._driver
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - 3_600_000  # 1 hour ago

    await _seed(driver, "visible", now_ms)  # standard + recent  -> included
    await _seed(driver, "secret", now_ms, classification="secret")  # excluded
    await _seed(driver, "localonly", now_ms, local_only=True)  # excluded
    await _seed(driver, "deleted", now_ms, deleted_at=now_ms)  # excluded
    await _seed(driver, "stale", now_ms, updated_ms=now_ms - 7_200_000)  # < cutoff

    manifest = await HotSubgraphBuilder(graph_client).build(
        device_id="itest-device", last_sync_at_ms=cutoff
    )

    ids = {n["node_id"] for n in manifest["nodes"]}
    assert "visible" in ids
    assert {"secret", "localonly", "deleted", "stale"}.isdisjoint(ids)

    assert manifest["is_full_sync"] is False
    assert len(manifest["manifest_hash"]) == 64


async def test_full_sync_includes_recent_standard_node(graph_client) -> None:
    from master.sync.hot_subgraph import HotSubgraphBuilder

    driver = graph_client._driver
    now_ms = int(time.time() * 1000)
    await _seed(driver, "fullsync-node", now_ms)

    # last_sync_at_ms=0 => full sync (cutoff falls back to the lookback window).
    manifest = await HotSubgraphBuilder(graph_client).build(
        device_id="itest-device", last_sync_at_ms=0
    )

    ids = {n["node_id"] for n in manifest["nodes"]}
    assert "fullsync-node" in ids
    assert manifest["is_full_sync"] is True
