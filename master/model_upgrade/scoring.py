"""
master.model_upgrade.scoring
============================
Pure scoring + decision functions for the model-upgrade pipeline.

Kept free of I/O so the regression-detection and promotion-decision rules
(ARCHITECTURE.md §13) are deterministic and unit-testable. The thresholds
mirror the architecture doc:

  - Regression:  >5% drop on the primary metric → warn; >15% → rollback flag.
  - Promotion:   auto-promote if candidate beats incumbent by >8%; human
                 confirmation if the gain is 3–8%; reject below 3%.
"""

from __future__ import annotations

from master.model_upgrade.types import (
    BenchmarkResult,
    PromotionDecision,
    PromotionOutcome,
    RegressionReport,
    ShadowEvalResult,
)

# ── Thresholds (fractions, not percentages) ───────────────────────────────────
REGRESSION_WARN_THRESHOLD = 0.05
REGRESSION_ROLLBACK_THRESHOLD = 0.15
PROMOTION_AUTO_THRESHOLD = 0.08
PROMOTION_CONFIRM_THRESHOLD = 0.03


def composite_score(
    accuracy: float,
    p95_latency_ms: float,
    cost_per_1k_usd: float,
    *,
    accuracy_weight: float = 0.7,
    latency_weight: float = 0.2,
    cost_weight: float = 0.1,
) -> float:
    """
    Combine accuracy, latency, and cost into a single 0.0–1.0 score.

    Latency and cost are inverted (lower is better) and normalised with a soft
    saturating curve so a fast/cheap model cannot mask poor accuracy. Weights
    default to the architecture's usage-weighted emphasis on accuracy.
    """
    acc = _clamp01(accuracy)
    # Saturating "goodness" for latency: 1.0 at 0ms, ~0.5 at 1000ms.
    latency_goodness = 1.0 / (1.0 + max(0.0, p95_latency_ms) / 1000.0)
    # Saturating goodness for cost: 1.0 at $0, ~0.5 at $0.01 / 1k tokens.
    cost_goodness = 1.0 / (1.0 + max(0.0, cost_per_1k_usd) / 0.01)
    raw = acc * accuracy_weight + latency_goodness * latency_weight + cost_goodness * cost_weight
    return _clamp01(raw)


def detect_regression(
    current: BenchmarkResult,
    baseline_score: float,
) -> RegressionReport:
    """
    Compare a fresh benchmark to a rolling baseline score.

    ``delta`` is the relative change. A baseline of 0.0 is treated as "no
    history", which can never be a regression.
    """
    delta = 0.0 if baseline_score <= 0.0 else (current.score - baseline_score) / baseline_score

    return RegressionReport(
        model_id=current.model_id,
        current_score=current.score,
        baseline_score=baseline_score,
        delta=delta,
        is_regression=delta < -REGRESSION_WARN_THRESHOLD,
        is_rollback=delta < -REGRESSION_ROLLBACK_THRESHOLD,
    )


def decide_promotion(result: ShadowEvalResult) -> PromotionOutcome:
    """
    Map a shadow-eval relative gain onto a promotion decision.

    Banding (per ARCHITECTURE.md §13):
      gain < 0           → REGRESSION (never promote)
      0 ≤ gain < 3%      → REJECT
      3% ≤ gain < 8%     → NEEDS_CONFIRMATION
      gain ≥ 8%          → AUTO_PROMOTE
    """
    gain = result.relative_gain

    if gain < 0.0:
        decision = PromotionDecision.REGRESSION
        reason = f"candidate {gain:.1%} worse than incumbent — not promoting"
    elif gain < PROMOTION_CONFIRM_THRESHOLD:
        decision = PromotionDecision.REJECT
        reason = f"gain {gain:.1%} below {PROMOTION_CONFIRM_THRESHOLD:.0%} threshold"
    elif gain < PROMOTION_AUTO_THRESHOLD:
        decision = PromotionDecision.NEEDS_CONFIRMATION
        reason = f"gain {gain:.1%} in human-review band — confirmation required"
    else:
        decision = PromotionDecision.AUTO_PROMOTE
        reason = f"gain {gain:.1%} exceeds auto-promote threshold"

    return PromotionOutcome(
        candidate_id=result.candidate_id,
        incumbent_id=result.incumbent_id,
        decision=decision,
        relative_gain=gain,
        reason=reason,
    )


def relative_gain(candidate_score: float, incumbent_score: float) -> float:
    """Relative improvement of candidate over incumbent. Incumbent 0 → 0 gain."""
    if incumbent_score <= 0.0:
        return 0.0
    return (candidate_score - incumbent_score) / incumbent_score


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
