"""
master.sync.conflict_resolver
==============================
Vector-clock-based last-write-wins for sync deltas, with agent-priority
tiebreaks (per ARCHITECTURE.md §12.3).

A conflict is detected when an incoming `NodeDelta` (or `EdgeDelta`) targets
the same node/edge as an existing record. The resolver picks a winner using:

  1. Compare Lamport vector_clock values (caller supplies clocks per agent
     for this device; a single int per delta is the simplification we use,
     as it tracks "time at the device that produced this write").
  2. If clocks are equal *and* one delta is from a higher-priority agent,
     the higher-priority agent wins.
  3. If clocks AND agents match, fall back to `updated_at` (later wins).

The resolver is pure: it does no IO. The caller is responsible for fetching
the current state, calling `resolve(...)`, and persisting the winner.

Audit hook: `ConflictResolution` records are returned alongside the winner
so the gRPC servicer can append them to the HMAC audit chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class AgentPriority(IntEnum):
    """
    Agent precedence for conflict resolution. Higher value wins.
    Mirrors ARCHITECTURE.md §12.3:
        Librarian > HealthAgent > FinancialAgent > Others
    """

    OTHER = 0
    FINANCIAL = 10
    HEALTH = 20
    LIBRARIAN = 30


_AGENT_PRIORITY: dict[str, AgentPriority] = {
    "librarian-agent": AgentPriority.LIBRARIAN,
    "health-agent": AgentPriority.HEALTH,
    "financial-agent": AgentPriority.FINANCIAL,
}


def _priority_for(agent_id: str | None) -> AgentPriority:
    if not agent_id:
        return AgentPriority.OTHER
    return _AGENT_PRIORITY.get(agent_id.lower(), AgentPriority.OTHER)


@dataclass(frozen=True)
class DeltaCandidate:
    """A unified representation of a NodeDelta or EdgeDelta for resolution."""

    record_id: str
    operation: str  # "upsert" | "soft_delete" | "delete"
    source_agent: str | None
    vector_clock: int  # Lamport clock at write time
    updated_at: int  # Unix ms; tiebreak when clocks AND agents tie
    payload_hash: str  # SHA-256 of encrypted payload (for audit)


@dataclass(frozen=True)
class ConflictResolution:
    """Audit record describing how a conflict was resolved."""

    record_id: str
    winner_agent: str | None
    winner_clock: int
    winner_hash: str
    loser_agent: str | None
    loser_clock: int
    loser_hash: str
    reason: str  # "clock" | "priority" | "timestamp" | "no_conflict"


class ConflictResolver:
    """
    Pure conflict resolution. No IO. Stateless; safe to share.
    """

    def resolve(
        self,
        incoming: DeltaCandidate,
        existing: DeltaCandidate | None,
    ) -> tuple[DeltaCandidate, ConflictResolution | None]:
        """
        Decide whether to accept the incoming delta.

        Returns:
            (winner, audit_record). When `existing` is None or the incoming
            delta is strictly newer, `audit_record` is None — only true
            conflicts produce one.
        """
        if existing is None:
            return incoming, None

        # 1. Vector clock comparison
        if incoming.vector_clock > existing.vector_clock:
            return incoming, self._audit(incoming, existing, "clock")
        if incoming.vector_clock < existing.vector_clock:
            return existing, self._audit(existing, incoming, "clock")

        # 2. Agent priority tiebreak
        ip = _priority_for(incoming.source_agent)
        ep = _priority_for(existing.source_agent)
        if ip > ep:
            return incoming, self._audit(incoming, existing, "priority")
        if ip < ep:
            return existing, self._audit(existing, incoming, "priority")

        # 3. Timestamp tiebreak (later wins)
        if incoming.updated_at >= existing.updated_at:
            return incoming, self._audit(incoming, existing, "timestamp")
        return existing, self._audit(existing, incoming, "timestamp")

    @staticmethod
    def _audit(
        winner: DeltaCandidate,
        loser: DeltaCandidate,
        reason: str,
    ) -> ConflictResolution:
        return ConflictResolution(
            record_id=winner.record_id,
            winner_agent=winner.source_agent,
            winner_clock=winner.vector_clock,
            winner_hash=winner.payload_hash,
            loser_agent=loser.source_agent,
            loser_clock=loser.vector_clock,
            loser_hash=loser.payload_hash,
            reason=reason,
        )


def candidate_from_node_delta(delta: Any) -> DeltaCandidate:
    """Build a DeltaCandidate from a `lucifer_sync_pb2.NodeDelta`."""
    import hashlib

    return DeltaCandidate(
        record_id=delta.node_id,
        operation=delta.operation,
        source_agent=delta.source_agent or None,
        vector_clock=getattr(delta, "vector_clock", 0),  # set on enclosing SyncMessage
        updated_at=delta.updated_at,
        payload_hash=hashlib.sha256(delta.payload).hexdigest() if delta.payload else "",
    )


def candidate_from_edge_delta(delta: Any, vector_clock: int = 0) -> DeltaCandidate:
    """Build a DeltaCandidate from a `lucifer_sync_pb2.EdgeDelta`."""
    import hashlib

    payload_hash = hashlib.sha256(
        f"{delta.from_node_id}->{delta.to_node_id}:{delta.relation}".encode()
    ).hexdigest()
    return DeltaCandidate(
        record_id=delta.edge_id,
        operation=delta.operation,
        source_agent=None,  # edges are not agent-attributed
        vector_clock=vector_clock,
        updated_at=delta.valid_from,
        payload_hash=payload_hash,
    )
