"""Unit tests for master.sync.hot_subgraph pure-logic helpers."""

from __future__ import annotations

import json

from master.sync.hot_subgraph import HotSubgraphBuilder


def _node(node_id: str, payload: bytes, classification: str = "standard") -> dict:
    return {
        "node_id": node_id,
        "operation": "upsert",
        "node_type": "Memory",
        "payload": payload,
        "classification": classification,
        "updated_at": 1000,
        "source_agent": "librarian",
    }


def _edge(edge_id: str, src: str, dst: str, relation: str = "RELATED_TO") -> dict:
    return {
        "edge_id": edge_id,
        "operation": "upsert",
        "from_node_id": src,
        "to_node_id": dst,
        "relation": relation,
        "weight": 1.0,
        "valid_from": 0,
        "valid_until": 0,
    }


# ── _format_node ──────────────────────────────────────────────────────────────


def test_format_node_strips_internal_fields():
    record = {
        "node_id": "n1",
        "node_type": "Memory",
        "classification": "standard",
        "updated_at": 5000,
        "source_agent": "librarian",
        "attrs": {
            "name": "hello",
            "classification": "standard",  # should be stripped
            "localOnly": False,  # should be stripped
            "deletedAt": None,  # should be stripped
            "sourceAgent": "librarian",  # should be stripped
        },
    }
    result = HotSubgraphBuilder._format_node(record)
    payload = json.loads(result["payload"])
    assert payload == {"name": "hello"}
    assert "classification" not in payload
    assert "localOnly" not in payload
    assert "deletedAt" not in payload
    assert "sourceAgent" not in payload


def test_format_node_canonical_json_sort_keys():
    record = {
        "node_id": "n1",
        "node_type": "Memory",
        "classification": "standard",
        "updated_at": 5000,
        "source_agent": "",
        "attrs": {"z": 1, "a": 2, "m": 3},
    }
    result = HotSubgraphBuilder._format_node(record)
    # Keys should be sorted in serialised payload
    assert result["payload"] == b'{"a": 2, "m": 3, "z": 1}'


def test_format_node_handles_unknown_node_type():
    record = {
        "node_id": "n1",
        "node_type": None,
        "classification": "standard",
        "updated_at": 0,
        "source_agent": None,
        "attrs": None,
    }
    result = HotSubgraphBuilder._format_node(record)
    assert result["node_type"] == "Unknown"
    assert result["source_agent"] == ""
    assert result["payload"] == b"{}"


# ── _apply_size_cap ───────────────────────────────────────────────────────────


def test_apply_size_cap_under_limit_returns_all():
    nodes = [_node("n1", b"a" * 10), _node("n2", b"b" * 10)]
    edges = [_edge("e1", "n1", "n2")]
    kept_nodes, kept_edges, capped = HotSubgraphBuilder._apply_size_cap(nodes, edges, 1000)
    assert kept_nodes == nodes
    assert kept_edges == edges
    assert capped is False


def test_apply_size_cap_drops_lowest_priority():
    # Nodes pre-ordered DESC by updatedAtMs (newest first); cap drops tail.
    nodes = [
        _node("newest", b"x" * 20),
        _node("middle", b"x" * 20),
        _node("oldest", b"x" * 20),
    ]
    edges = []
    kept_nodes, _, capped = HotSubgraphBuilder._apply_size_cap(nodes, edges, 25)
    assert capped is True
    assert len(kept_nodes) == 1
    assert kept_nodes[0]["node_id"] == "newest"


def test_apply_size_cap_prunes_orphan_edges():
    nodes = [_node("n1", b"x" * 20), _node("n2", b"x" * 20), _node("n3", b"x" * 20)]
    edges = [
        _edge("e_keep", "n1", "n2"),  # both endpoints in kept set
        _edge("e_drop1", "n1", "n3"),  # n3 dropped
        _edge("e_drop2", "n3", "n2"),  # n3 dropped
    ]
    kept_nodes, kept_edges, capped = HotSubgraphBuilder._apply_size_cap(nodes, edges, 45)
    kept_ids = {n["node_id"] for n in kept_nodes}
    assert capped is True
    assert kept_ids == {"n1", "n2"}
    assert [e["edge_id"] for e in kept_edges] == ["e_keep"]


def test_apply_size_cap_zero_cap_drops_everything():
    nodes = [_node("n1", b"x" * 10)]
    kept_nodes, kept_edges, capped = HotSubgraphBuilder._apply_size_cap(nodes, [], 0)
    assert kept_nodes == []
    assert kept_edges == []
    assert capped is True


# ── _hash_manifest ────────────────────────────────────────────────────────────


def test_hash_manifest_deterministic():
    nodes = [_node("n1", b"alpha"), _node("n2", b"beta")]
    edges = [_edge("e1", "n1", "n2")]
    h1 = HotSubgraphBuilder._hash_manifest(nodes, edges)
    h2 = HotSubgraphBuilder._hash_manifest(nodes, edges)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_manifest_changes_on_payload_change():
    nodes_a = [_node("n1", b"alpha")]
    nodes_b = [_node("n1", b"beta")]
    assert HotSubgraphBuilder._hash_manifest(nodes_a, []) != HotSubgraphBuilder._hash_manifest(
        nodes_b, []
    )


def test_hash_manifest_changes_on_edge_relation_change():
    nodes = [_node("n1", b"x"), _node("n2", b"y")]
    edges_a = [_edge("e1", "n1", "n2", relation="RELATED_TO")]
    edges_b = [_edge("e1", "n1", "n2", relation="DERIVED_FROM")]
    assert HotSubgraphBuilder._hash_manifest(nodes, edges_a) != HotSubgraphBuilder._hash_manifest(
        nodes, edges_b
    )


def test_hash_manifest_empty_inputs_stable():
    h = HotSubgraphBuilder._hash_manifest([], [])
    assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
