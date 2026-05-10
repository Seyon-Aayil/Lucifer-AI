"""Unit tests for master.agents.librarian.memory_writer classification routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from master.agents.base.agent import MemoryDelta
from master.agents.librarian.memory_writer import MemoryWriter, _delta_to_text


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_graph_client():
    gc = AsyncMock()
    gc.upsert_node = AsyncMock(return_value=None)
    gc.soft_delete_node = AsyncMock(return_value=None)
    gc.upsert_edge = AsyncMock(return_value=None)
    return gc


@pytest.fixture
def writer(mock_graph_client):
    with (
        patch("master.agents.librarian.memory_writer.Mem0Client") as mem0_cls,
        patch("master.agents.librarian.memory_writer.ZepClient") as zep_cls,
    ):
        mem0_cls.return_value.add = AsyncMock(return_value=None)
        zep_cls.return_value.add_episode = AsyncMock(return_value=None)
        w = MemoryWriter(graph_client=mock_graph_client)
    return w


def _delta(classification: str = "standard", op: str = "upsert") -> MemoryDelta:
    return MemoryDelta(
        operation=op,
        node_type="Memory",
        node_id="m1",
        attributes={"name": "test note"},
        classification=classification,
    )


# ── Routing by classification ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_standard_writes_all_three_stores(writer, mock_graph_client):
    await writer.apply_deltas([_delta("standard")])
    mock_graph_client.upsert_node.assert_awaited_once()
    writer._mem0.add.assert_awaited_once()
    writer._zep.add_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_writes_all_three_stores(writer, mock_graph_client):
    await writer.apply_deltas([_delta("public")])
    mock_graph_client.upsert_node.assert_awaited_once()
    writer._mem0.add.assert_awaited_once()
    writer._zep.add_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_restricted_skips_mem0(writer, mock_graph_client):
    await writer.apply_deltas([_delta("restricted")])
    mock_graph_client.upsert_node.assert_awaited_once()
    writer._mem0.add.assert_not_awaited()
    writer._zep.add_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_secret_writes_to_no_store(writer, mock_graph_client):
    await writer.apply_deltas([_delta("secret")])
    mock_graph_client.upsert_node.assert_not_awaited()
    writer._mem0.add.assert_not_awaited()
    writer._zep.add_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_classification_case_insensitive(writer, mock_graph_client):
    await writer.apply_deltas([_delta("SECRET")])
    mock_graph_client.upsert_node.assert_not_awaited()
    writer._mem0.add.assert_not_awaited()


# ── Operation dispatch ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_routes_to_neo4j(writer, mock_graph_client):
    delta = MemoryDelta(operation="soft_delete", node_id="m1", classification="standard")
    await writer.apply_deltas([delta])
    mock_graph_client.soft_delete_node.assert_awaited_once_with("m1")
    mock_graph_client.upsert_node.assert_not_awaited()


@pytest.mark.asyncio
async def test_edge_upsert_routes_to_neo4j(writer, mock_graph_client):
    delta = MemoryDelta(
        operation="edge_upsert",
        from_node_id="a",
        to_node_id="b",
        edge_relation="RELATED_TO",
        classification="standard",
    )
    await writer.apply_deltas([delta])
    mock_graph_client.upsert_edge.assert_awaited_once_with("a", "b", "RELATED_TO")


@pytest.mark.asyncio
async def test_edge_upsert_no_text_to_mem0_zep(writer, mock_graph_client):
    delta = MemoryDelta(
        operation="edge_upsert",
        from_node_id="a",
        to_node_id="b",
        edge_relation="RELATED_TO",
        classification="standard",
    )
    await writer.apply_deltas([delta])
    # Edges have no text representation → Mem0/Zep not called
    writer._mem0.add.assert_not_awaited()
    writer._zep.add_episode.assert_not_awaited()


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_deltas_returns_immediately(writer, mock_graph_client):
    await writer.apply_deltas([])
    mock_graph_client.upsert_node.assert_not_awaited()
    writer._mem0.add.assert_not_awaited()
    writer._zep.add_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_logs_but_does_not_raise(writer, mock_graph_client):
    mock_graph_client.upsert_node.side_effect = RuntimeError("neo4j down")
    # Should NOT raise — failures swallowed via gather(return_exceptions=True)
    await writer.apply_deltas([_delta("standard")])
    writer._mem0.add.assert_awaited_once()
    writer._zep.add_episode.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_graph_client_skips_neo4j(mock_graph_client):
    with (
        patch("master.agents.librarian.memory_writer.Mem0Client") as mem0_cls,
        patch("master.agents.librarian.memory_writer.ZepClient") as zep_cls,
    ):
        mem0_cls.return_value.add = AsyncMock()
        zep_cls.return_value.add_episode = AsyncMock()
        writer = MemoryWriter(graph_client=None)
    await writer.apply_deltas([_delta("standard")])
    # Graph client is None — Neo4j skipped; cloud stores still receive writes
    writer._mem0.add.assert_awaited_once()
    writer._zep.add_episode.assert_awaited_once()


# ── _delta_to_text ────────────────────────────────────────────────────────────


def test_delta_to_text_uses_name_attribute():
    delta = MemoryDelta(
        operation="upsert", node_type="Note", node_id="n1", attributes={"name": "hello"}
    )
    assert _delta_to_text(delta) == "Note 'hello' updated (id=n1)"


def test_delta_to_text_falls_back_to_title():
    delta = MemoryDelta(
        operation="upsert", node_type="Doc", node_id="d1", attributes={"title": "MyDoc"}
    )
    assert _delta_to_text(delta) == "Doc 'MyDoc' updated (id=d1)"


def test_delta_to_text_falls_back_to_content():
    delta = MemoryDelta(
        operation="upsert", node_type="Msg", node_id="m1", attributes={"content": "ping"}
    )
    assert _delta_to_text(delta) == "Msg 'ping' updated (id=m1)"


def test_delta_to_text_no_named_attrs():
    delta = MemoryDelta(operation="upsert", node_type="X", node_id="x1", attributes={"foo": "bar"})
    assert _delta_to_text(delta) == "X id=x1 updated"


def test_delta_to_text_soft_delete():
    delta = MemoryDelta(operation="soft_delete", node_id="n1")
    assert _delta_to_text(delta) == "Node n1 deleted"


def test_delta_to_text_edge_returns_empty():
    delta = MemoryDelta(operation="edge_upsert", from_node_id="a", to_node_id="b")
    assert _delta_to_text(delta) == ""
