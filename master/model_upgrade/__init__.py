"""
master.model_upgrade
====================
Model upgrade subsystem: nightly benchmark → regression detection → shadow
evaluation → OTA promotion. See ARCHITECTURE.md §13.

The package is split into a pure core (``types``, ``scoring``) that is fully
unit-testable without I/O, and runner classes (``benchmark``, ``promoter``)
that take injected async callables for the actual LLM dispatch — keeping all
inference behind the ProviderRegistry per AGENTS.md.
"""

from __future__ import annotations

from master.model_upgrade.benchmark import BenchmarkRunner, BenchmarkTask
from master.model_upgrade.promoter import (
    ModelPromoter,
    ReplayQuery,
    ShadowEvaluator,
)
from master.model_upgrade.scoring import (
    composite_score,
    decide_promotion,
    detect_regression,
    relative_gain,
)
from master.model_upgrade.types import (
    BenchmarkResult,
    PromotionDecision,
    PromotionOutcome,
    RegressionReport,
    ShadowEvalResult,
)

__all__ = [
    "BenchmarkResult",
    "BenchmarkRunner",
    "BenchmarkTask",
    "ModelPromoter",
    "PromotionDecision",
    "PromotionOutcome",
    "RegressionReport",
    "ReplayQuery",
    "ShadowEvalResult",
    "ShadowEvaluator",
    "composite_score",
    "decide_promotion",
    "detect_regression",
    "relative_gain",
]
