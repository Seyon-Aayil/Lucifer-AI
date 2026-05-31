"""
master.model_upgrade.benchmark
==============================
Benchmark runner: scores a model over a fixed task suite and records the
result, then checks it against a rolling baseline for regressions.

The runner is provider-agnostic: it takes a ``BenchmarkTask`` list and an
async ``grade`` callable so the actual LLM dispatch (always through the
ProviderRegistry per AGENTS.md) is injected by the caller rather than imported
here. This keeps the scoring path unit-testable without network access.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.model_upgrade.scoring import composite_score, detect_regression
from master.model_upgrade.types import BenchmarkResult, RegressionReport

log = get_logger(__name__)
tracer = get_tracer(__name__)


@dataclass(frozen=True)
class BenchmarkTask:
    """A single graded benchmark item (MMLU-style or domain task)."""

    task_id: str
    prompt: str
    expected: str
    suite: str = "default"


# grade(task, model_id) -> (correct: bool, latency_ms: float, cost_usd: float)
GradeFn = Callable[[BenchmarkTask, str], Awaitable[tuple[bool, float, float]]]


class BenchmarkRunner:
    """
    Runs a benchmark suite against a single model and produces a
    ``BenchmarkResult``. Stateless apart from the injected grade function.
    """

    def __init__(self, grade: GradeFn) -> None:
        self._grade = grade

    async def run(self, model_id: str, tasks: list[BenchmarkTask]) -> BenchmarkResult:
        """Grade every task for ``model_id`` and aggregate into a result."""
        with tracer.start_as_current_span("model_upgrade.benchmark.run"):
            if not tasks:
                log.warning("model_upgrade.benchmark.no_tasks", model_id=model_id)
                return BenchmarkResult(
                    model_id=model_id,
                    score=0.0,
                    accuracy=0.0,
                    p50_latency_ms=0.0,
                    p95_latency_ms=0.0,
                    cost_per_1k_usd=0.0,
                    sample_count=0,
                    ran_at=datetime.now(UTC),
                )

            correct = 0
            latencies: list[float] = []
            total_cost = 0.0
            for task in tasks:
                ok, latency_ms, cost_usd = await self._grade(task, model_id)
                correct += int(ok)
                latencies.append(latency_ms)
                total_cost += cost_usd

            n = len(tasks)
            accuracy = correct / n
            latencies.sort()
            p50 = _percentile(latencies, 0.50)
            p95 = _percentile(latencies, 0.95)
            cost_per_1k = (total_cost / n) * 1000.0
            score = composite_score(accuracy, p95, cost_per_1k)

            result = BenchmarkResult(
                model_id=model_id,
                score=score,
                accuracy=accuracy,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                cost_per_1k_usd=cost_per_1k,
                sample_count=n,
                ran_at=datetime.now(UTC),
            )
            log.info(
                "model_upgrade.benchmark.complete",
                model_id=model_id,
                score=round(score, 4),
                accuracy=round(accuracy, 4),
                p95_ms=round(p95, 1),
                samples=n,
            )
            return result

    def check_regression(self, result: BenchmarkResult, baseline_score: float) -> RegressionReport:
        """Compare a result to the rolling baseline and emit a warning on drop."""
        report = detect_regression(result, baseline_score)
        if report.is_rollback:
            log.error(
                "model_upgrade.regression.rollback",
                model_id=result.model_id,
                delta=round(report.delta, 4),
            )
        elif report.is_regression:
            log.warning(
                "model_upgrade.regression.warn",
                model_id=result.model_id,
                delta=round(report.delta, 4),
            )
        return report


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list. Empty → 0.0."""
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]
