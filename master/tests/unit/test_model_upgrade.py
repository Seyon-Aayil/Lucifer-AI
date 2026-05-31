"""Unit tests for master.model_upgrade scoring, regression, and promotion logic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from master.model_upgrade.benchmark import BenchmarkRunner, BenchmarkTask, _percentile
from master.model_upgrade.promoter import ModelPromoter, ReplayQuery, ShadowEvaluator
from master.model_upgrade.scoring import (
    PROMOTION_AUTO_THRESHOLD,
    composite_score,
    decide_promotion,
    detect_regression,
    relative_gain,
)
from master.model_upgrade.types import (
    BenchmarkResult,
    PromotionDecision,
    ShadowEvalResult,
)


def _result(score: float, model_id: str = "m") -> BenchmarkResult:
    return BenchmarkResult(
        model_id=model_id,
        score=score,
        accuracy=score,
        p50_latency_ms=100.0,
        p95_latency_ms=200.0,
        cost_per_1k_usd=0.001,
        sample_count=10,
        ran_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── composite_score ───────────────────────────────────────────────────────────


def test_composite_score_in_range():
    s = composite_score(accuracy=0.9, p95_latency_ms=200.0, cost_per_1k_usd=0.001)
    assert 0.0 <= s <= 1.0


def test_composite_score_accuracy_dominates():
    high_acc = composite_score(0.95, p95_latency_ms=2000.0, cost_per_1k_usd=0.05)
    low_acc = composite_score(0.30, p95_latency_ms=10.0, cost_per_1k_usd=0.0)
    assert high_acc > low_acc


def test_composite_score_clamps_accuracy():
    assert composite_score(2.0, 0.0, 0.0) <= 1.0
    assert composite_score(-1.0, 0.0, 0.0) >= 0.0


def test_composite_score_lower_latency_better():
    fast = composite_score(0.8, p95_latency_ms=50.0, cost_per_1k_usd=0.001)
    slow = composite_score(0.8, p95_latency_ms=5000.0, cost_per_1k_usd=0.001)
    assert fast > slow


# ── detect_regression ─────────────────────────────────────────────────────────


def test_regression_no_baseline_is_not_regression():
    report = detect_regression(_result(0.5), baseline_score=0.0)
    assert report.delta == 0.0
    assert not report.is_regression
    assert not report.is_rollback


def test_regression_small_drop_warns_not_rollback():
    # 10% drop → warn (>5%) but not rollback (<15%)
    report = detect_regression(_result(0.9), baseline_score=1.0)
    assert report.is_regression
    assert not report.is_rollback


def test_regression_large_drop_flags_rollback():
    # 20% drop → rollback
    report = detect_regression(_result(0.8), baseline_score=1.0)
    assert report.is_regression
    assert report.is_rollback


def test_regression_improvement_is_not_regression():
    report = detect_regression(_result(1.1), baseline_score=1.0)
    assert report.delta > 0
    assert not report.is_regression


# ── relative_gain ─────────────────────────────────────────────────────────────


def test_relative_gain_basic():
    assert relative_gain(1.1, 1.0) == pytest.approx(0.1)


def test_relative_gain_zero_incumbent():
    assert relative_gain(0.5, 0.0) == 0.0


# ── decide_promotion ──────────────────────────────────────────────────────────


def _shadow(gain: float) -> ShadowEvalResult:
    return ShadowEvalResult(
        candidate_id="cand",
        incumbent_id="inc",
        candidate_score=1.0 + gain,
        incumbent_score=1.0,
        relative_gain=gain,
        sample_count=100,
    )


def test_decide_promotion_auto():
    outcome = decide_promotion(_shadow(0.10))
    assert outcome.decision is PromotionDecision.AUTO_PROMOTE


def test_decide_promotion_confirmation_band():
    outcome = decide_promotion(_shadow(0.05))
    assert outcome.decision is PromotionDecision.NEEDS_CONFIRMATION


def test_decide_promotion_reject_small_gain():
    outcome = decide_promotion(_shadow(0.01))
    assert outcome.decision is PromotionDecision.REJECT


def test_decide_promotion_regression():
    outcome = decide_promotion(_shadow(-0.05))
    assert outcome.decision is PromotionDecision.REGRESSION


def test_decide_promotion_boundary_is_auto():
    outcome = decide_promotion(_shadow(PROMOTION_AUTO_THRESHOLD))
    assert outcome.decision is PromotionDecision.AUTO_PROMOTE


# ── _percentile ───────────────────────────────────────────────────────────────


def test_percentile_empty_is_zero():
    assert _percentile([], 0.95) == 0.0


def test_percentile_p50_and_p95():
    values = [float(i) for i in range(1, 101)]  # 1..100 sorted
    assert _percentile(values, 0.50) == pytest.approx(50.0, abs=1.0)
    assert _percentile(values, 0.95) == pytest.approx(95.0, abs=1.0)


# ── BenchmarkRunner (async, injected grade fn) ────────────────────────────────


async def test_benchmark_runner_aggregates():
    async def grade(task: BenchmarkTask, model_id: str) -> tuple[bool, float, float]:
        # Half correct, fixed latency/cost.
        return (task.task_id.endswith("0"), 100.0, 0.000001)

    tasks = [BenchmarkTask(task_id=f"t{i}", prompt="p", expected="e") for i in range(10)]
    runner = BenchmarkRunner(grade=grade)
    result = await runner.run("model-x", tasks)

    assert result.sample_count == 10
    assert result.accuracy == pytest.approx(0.1)  # only t0 ends with 0... t10 excluded (range 10)
    assert 0.0 <= result.score <= 1.0


async def test_benchmark_runner_empty_tasks():
    async def grade(task: BenchmarkTask, model_id: str) -> tuple[bool, float, float]:
        return (True, 0.0, 0.0)

    runner = BenchmarkRunner(grade=grade)
    result = await runner.run("model-x", [])
    assert result.sample_count == 0
    assert result.score == 0.0


# ── ShadowEvaluator + ModelPromoter (async, injected hooks) ───────────────────


async def test_shadow_evaluator_computes_gain():
    async def generate(prompt: str, model_id: str) -> str:
        return "candidate-answer"

    async def score(prompt: str, response: str) -> float:
        # Candidate scores higher than the incumbent's cached response.
        return 0.9 if response == "candidate-answer" else 0.6

    queries = [
        ReplayQuery(query_id=f"q{i}", prompt="p", incumbent_response="old") for i in range(5)
    ]
    evaluator = ShadowEvaluator(generate=generate, score=score)
    result = await evaluator.evaluate("cand", "inc", queries)

    assert result.candidate_score == pytest.approx(0.9)
    assert result.incumbent_score == pytest.approx(0.6)
    assert result.relative_gain == pytest.approx(0.5)


async def test_model_promoter_auto_promote_activates():
    activated: list[str] = []
    confirmations: list[str] = []

    async def activate(model_id: str) -> None:
        activated.append(model_id)

    async def request_confirmation(outcome) -> None:  # type: ignore[no-untyped-def]
        confirmations.append(outcome.candidate_id)

    promoter = ModelPromoter(activate=activate, request_confirmation=request_confirmation)
    outcome = await promoter.maybe_promote(_shadow(0.20))

    assert outcome.decision is PromotionDecision.AUTO_PROMOTE
    assert activated == ["cand"]
    assert confirmations == []


async def test_model_promoter_confirmation_band_requests_signoff():
    activated: list[str] = []
    confirmations: list[str] = []

    async def activate(model_id: str) -> None:
        activated.append(model_id)

    async def request_confirmation(outcome) -> None:  # type: ignore[no-untyped-def]
        confirmations.append(outcome.candidate_id)

    promoter = ModelPromoter(activate=activate, request_confirmation=request_confirmation)
    outcome = await promoter.maybe_promote(_shadow(0.05))

    assert outcome.decision is PromotionDecision.NEEDS_CONFIRMATION
    assert activated == []
    assert confirmations == ["cand"]


async def test_model_promoter_reject_is_noop():
    activated: list[str] = []
    confirmations: list[str] = []

    async def activate(model_id: str) -> None:
        activated.append(model_id)

    async def request_confirmation(outcome) -> None:  # type: ignore[no-untyped-def]
        confirmations.append(outcome.candidate_id)

    promoter = ModelPromoter(activate=activate, request_confirmation=request_confirmation)
    outcome = await promoter.maybe_promote(_shadow(0.01))

    assert outcome.decision is PromotionDecision.REJECT
    assert activated == []
    assert confirmations == []
