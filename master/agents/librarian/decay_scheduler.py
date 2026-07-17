"""
master.agents.librarian.decay_scheduler
========================================
APScheduler-based nightly memory decay pass.

Algorithm:
  reference  = max(updatedAt, lastAccessedAt)   # write OR read refreshes a node
  new_score  = exp(-DECAY_RATE * days_since_reference)

The score is a pure function of recency — it is NOT derived from the previous
score. This matters: the previous implementation computed

    new_score = current_score * exp(-DECAY_RATE * days_since_update)

which multiplied the *already-decayed* score by a factor covering the *total*
elapsed time since the write. Run nightly, that compounds to
exp(-rate * N(N+1)/2) — quadratic decay — and soft-deleted any node not
*written* to for ~8 days, against a documented intent of ~30. See ADR-011.

Computing from scratch is idempotent: a missed or double-run nightly job
cannot corrupt the score, which matters because the scheduler is in-process
APScheduler with no durable run-ledger.

`lastAccessedAt` is stamped by GraphClient on read (debounced server-side to at
most one write per node per hour), so a node that is read often survives even if
it is never written again. Without it this is a write-recency model, not a decay
model — reads would not keep memories alive.

Nodes whose decayScore falls below DELETE_THRESHOLD are soft-deleted
(deletedAt timestamp set; no DETACH DELETE preserves edge history). Soft-delete
is the only reason a decay bug is recoverable rather than a post-mortem — do not
replace it with a hard delete.

Runs nightly at 02:00 via AsyncIOScheduler.
Can also be triggered on-demand via run_decay_cycle() for testing.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from master.core.logging import get_logger
from master.core.telemetry import get_tracer

if TYPE_CHECKING:
    from master.agents.librarian.graph_client import GraphClient

log = get_logger(__name__)
tracer = get_tracer(__name__)

# Exponential decay rate: score halves every 7 days (ln(2)/7 ≈ 0.099)
_DECAY_RATE: float = math.log(2) / 7
# Nodes below this score are soft-deleted
_DELETE_THRESHOLD: float = 0.05
# Score of a node touched right now. Derived: exp(-rate * 0) == 1.0.
_INITIAL_SCORE: float = 1.0
# Days of no read AND no write before a node is soft-deleted. Derived from the
# rate and threshold above rather than hard-coded, so the three can never drift:
#   exp(-rate * d) < threshold  ⟺  d > ln(1/threshold) / rate  ≈ 30.3
# Pinned by test_soft_delete_horizon_is_30_days.
SOFT_DELETE_HORIZON_DAYS: float = math.log(1.0 / _DELETE_THRESHOLD) / _DECAY_RATE


class DecayScheduler:
    """
    Manages periodic decay of stale knowledge-graph nodes.
    Attach to an AsyncIOScheduler in the FastAPI lifespan for automatic runs.
    """

    def __init__(self, graph_client: GraphClient) -> None:
        self._gc = graph_client

    # ── Scheduling ────────────────────────────────────────────────────────────

    def attach(self, scheduler: Any, hour: int = 2, minute: int = 0) -> None:
        """
        Register the nightly decay job on an existing AsyncIOScheduler.
        Default: runs at 02:00 every day.

        Usage in lifespan:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            scheduler = AsyncIOScheduler()
            decay_scheduler.attach(scheduler)
            scheduler.start()
        """
        scheduler.add_job(
            self.run_decay_cycle,
            trigger="cron",
            hour=hour,
            minute=minute,
            id="memory_decay",
            replace_existing=True,
        )
        log.info("decay_scheduler.attached", hour=hour, minute=minute)

    # ── Decay pass ────────────────────────────────────────────────────────────

    async def run_decay_cycle(self) -> dict[str, int]:
        """
        Execute a single decay pass over all tracked nodes.
        Returns a summary dict: {updated, soft_deleted, errors}.
        """
        with tracer.start_as_current_span("decay_scheduler.run_cycle"):
            log.info("decay_scheduler.cycle.started")
            now = datetime.now(UTC)
            stats: dict[str, int] = {"updated": 0, "soft_deleted": 0, "errors": 0}

            try:
                nodes = await self._fetch_tracked_nodes()
            except Exception as exc:
                log.error("decay_scheduler.fetch_failed", error=str(exc))
                return stats

            new_scores: dict[str, float] = {}
            to_soft_delete: list[str] = []

            for node in nodes:
                node_id: str = node.get("id", "")
                if not node_id:
                    continue

                # Reference = the most recent evidence the node still matters.
                # A read counts as much as a write; see module docstring.
                reference = _latest(
                    _parse_dt(node.get("updatedAt")),
                    _parse_dt(node.get("lastAccessedAt")),
                )
                if reference is None:
                    continue

                days_elapsed = max(0.0, (now - reference).total_seconds() / 86400.0)
                # Computed from scratch, NOT from the previous score — see ADR-011.
                new_score = math.exp(-_DECAY_RATE * days_elapsed)

                if new_score < _DELETE_THRESHOLD:
                    to_soft_delete.append(node_id)
                else:
                    new_scores[node_id] = new_score

            # Batch-update scores for surviving nodes
            if new_scores:
                try:
                    await self._gc.update_decay_scores(new_scores)
                    stats["updated"] = len(new_scores)
                except Exception as exc:
                    log.error("decay_scheduler.update_failed", error=str(exc))
                    stats["errors"] += 1

            # Soft-delete nodes below threshold
            for node_id in to_soft_delete:
                try:
                    await self._gc.soft_delete_node(node_id)
                    stats["soft_deleted"] += 1
                except Exception as exc:
                    log.warning("decay_scheduler.soft_delete_failed", node=node_id, error=str(exc))
                    stats["errors"] += 1

            log.info("decay_scheduler.cycle.complete", **stats)
            return stats

    async def _fetch_tracked_nodes(self) -> list[dict[str, Any]]:
        """
        Fetch all live nodes eligible for decay scoring.

        A node qualifies if it carries either timestamp: `lastAccessedAt` alone
        is sufficient, so a node read but never re-written is still scored.
        """
        async with self._gc._driver.session() as session:
            result = await session.run(
                """
                MATCH (n)
                WHERE n.deletedAt IS NULL
                  AND (n.updatedAt IS NOT NULL OR n.lastAccessedAt IS NOT NULL)
                RETURN n.id AS id,
                       n.decayScore AS decayScore,
                       n.updatedAt AS updatedAt,
                       n.lastAccessedAt AS lastAccessedAt
                """
            )
            records = await result.data()
            return [dict(r) for r in records]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _latest(*values: datetime | None) -> datetime | None:
    """Return the most recent of the given datetimes, ignoring None. None if all are None."""
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _parse_dt(value: Any) -> datetime | None:
    """Parse a Neo4j DateTime or ISO string into a timezone-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        iso = str(value)
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return None
