"""Unit tests for master.sync.conflict_resolver."""

from __future__ import annotations

from typing import Any

from master.sync.conflict_resolver import (
    AgentPriority,
    ConflictResolver,
    DeltaCandidate,
    _priority_for,
)


def _cand(
    record_id: str = "node-1",
    operation: str = "upsert",
    source_agent: str | None = None,
    vector_clock: int = 1,
    updated_at: int = 1000,
    payload_hash: str = "abc",
) -> DeltaCandidate:
    return DeltaCandidate(
        record_id=record_id,
        operation=operation,
        source_agent=source_agent,
        vector_clock=vector_clock,
        updated_at=updated_at,
        payload_hash=payload_hash,
    )


resolver = ConflictResolver()


class TestAgentPriority:
    def test_librarian_highest(self) -> None:
        assert _priority_for("librarian-agent") == AgentPriority.LIBRARIAN

    def test_health_above_financial(self) -> None:
        assert _priority_for("health-agent") > _priority_for("financial-agent")

    def test_unknown_agent_is_other(self) -> None:
        assert _priority_for("my-custom-agent") == AgentPriority.OTHER

    def test_none_agent_is_other(self) -> None:
        assert _priority_for(None) == AgentPriority.OTHER


class TestConflictResolver:
    def test_no_existing_returns_incoming(self) -> None:
        incoming = _cand(vector_clock=5)
        winner, audit = resolver.resolve(incoming, None)
        assert winner is incoming
        assert audit is None

    def test_higher_clock_wins(self) -> None:
        incoming = _cand(vector_clock=10)
        existing = _cand(vector_clock=5)
        winner, audit = resolver.resolve(incoming, existing)
        assert winner is incoming
        assert audit is not None
        assert audit.reason == "clock"

    def test_lower_clock_loses(self) -> None:
        incoming = _cand(vector_clock=3)
        existing = _cand(vector_clock=9)
        winner, audit = resolver.resolve(incoming, existing)
        assert winner is existing
        assert audit is not None
        assert audit.reason == "clock"

    def test_equal_clock_agent_priority_wins(self) -> None:
        incoming = _cand(vector_clock=5, source_agent="librarian-agent")
        existing = _cand(vector_clock=5, source_agent="financial-agent")
        winner, audit = resolver.resolve(incoming, existing)
        assert winner is incoming
        assert audit is not None
        assert audit.reason == "priority"

    def test_equal_clock_equal_priority_timestamp_wins(self) -> None:
        incoming = _cand(vector_clock=5, source_agent=None, updated_at=2000)
        existing = _cand(vector_clock=5, source_agent=None, updated_at=1000)
        winner, audit = resolver.resolve(incoming, existing)
        assert winner is incoming
        assert audit is not None
        assert audit.reason == "timestamp"

    def test_equal_everything_incoming_wins(self) -> None:
        incoming = _cand(vector_clock=5, updated_at=1000)
        existing = _cand(vector_clock=5, updated_at=1000)
        winner, audit = resolver.resolve(incoming, existing)
        # Tie — incoming wins (>=)
        assert winner is incoming

    def test_audit_record_fields(self) -> None:
        incoming = _cand(source_agent="librarian-agent", vector_clock=10, payload_hash="win")
        existing = _cand(source_agent="financial-agent", vector_clock=5, payload_hash="lose")
        winner, audit = resolver.resolve(incoming, existing)
        assert audit is not None
        assert audit.winner_agent == "librarian-agent"
        assert audit.loser_agent == "financial-agent"
        assert audit.winner_hash == "win"
        assert audit.loser_hash == "lose"


class TestHotSubgraphSizeCap:
    """Test the size-cap logic in isolation (no Neo4j)."""

    def test_under_cap_no_truncation(self) -> None:
        from master.sync.hot_subgraph import HotSubgraphBuilder

        nodes = [
            {
                "node_id": f"n{i}",
                "payload": b"x" * 100,
                "updated_at": i,
                "operation": "upsert",
                "node_type": "Memory",
                "classification": "standard",
                "source_agent": "",
            }
            for i in range(5)
        ]
        edges: list[dict[str, Any]] = []
        result_nodes, result_edges, capped = HotSubgraphBuilder._apply_size_cap(
            nodes, edges, cap_bytes=10_000
        )
        assert len(result_nodes) == 5
        assert not capped

    def test_over_cap_truncates(self) -> None:
        from master.sync.hot_subgraph import HotSubgraphBuilder

        nodes = [
            {
                "node_id": f"n{i}",
                "payload": b"x" * 200,
                "updated_at": i,
                "operation": "upsert",
                "node_type": "Memory",
                "classification": "standard",
                "source_agent": "",
            }
            for i in range(10)
        ]
        edges: list[dict[str, Any]] = []
        result_nodes, _, capped = HotSubgraphBuilder._apply_size_cap(nodes, edges, cap_bytes=500)
        assert len(result_nodes) < 10
        assert capped

    def test_manifest_hash_deterministic(self) -> None:
        from master.sync.hot_subgraph import HotSubgraphBuilder

        nodes = [{"node_id": "n1", "payload": b"data"}]
        edges: list[dict[str, Any]] = []
        h1 = HotSubgraphBuilder._hash_manifest(nodes, edges)
        h2 = HotSubgraphBuilder._hash_manifest(nodes, edges)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex
