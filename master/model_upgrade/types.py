"""
master.model_upgrade.types
===========================
Value types for the model-upgrade subsystem (benchmark → shadow eval → OTA).

These are pure dataclasses with no I/O so the scoring / promotion logic in the
rest of the package stays trivially unit-testable. See ARCHITECTURE.md §13.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class PromotionDecision(enum.StrEnum):
    """Outcome of evaluating a candidate model against the incumbent."""

    AUTO_PROMOTE = "auto_promote"  # gain exceeds the auto threshold
    NEEDS_CONFIRMATION = "needs_confirmation"  # gain in the human-review band
    REJECT = "reject"  # no meaningful gain
    REGRESSION = "regression"  # candidate is worse — never promote


@dataclass(frozen=True)
class BenchmarkResult:
    """
    Aggregate score for a single model over a benchmark suite.

    ``score`` is the headline 0.0–1.0 composite (accuracy-weighted). The
    component metrics are retained for telemetry and regression analysis.
    """

    model_id: str
    score: float
    accuracy: float
    p50_latency_ms: float
    p95_latency_ms: float
    cost_per_1k_usd: float
    sample_count: int
    ran_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RegressionReport:
    """Result of comparing a fresh benchmark against a rolling baseline."""

    model_id: str
    current_score: float
    baseline_score: float
    delta: float  # current - baseline (negative = regression)
    is_regression: bool  # delta < -warn_threshold
    is_rollback: bool  # delta < -rollback_threshold


@dataclass(frozen=True)
class ShadowEvalResult:
    """Candidate-vs-incumbent comparison over replayed real queries."""

    candidate_id: str
    incumbent_id: str
    candidate_score: float
    incumbent_score: float
    relative_gain: float  # (candidate - incumbent) / incumbent
    sample_count: int


@dataclass(frozen=True)
class PromotionOutcome:
    """Final promotion verdict for a candidate model."""

    candidate_id: str
    incumbent_id: str
    decision: PromotionDecision
    relative_gain: float
    reason: str
