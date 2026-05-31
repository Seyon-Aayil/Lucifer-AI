"""
master.model_upgrade.promoter
=============================
Shadow evaluation + OTA promotion logic.

``ShadowEvaluator`` replays recent real queries against a candidate model with
zero user impact, scores both candidate and incumbent, and produces a
``PromotionOutcome``. ``ModelPromoter`` turns that outcome into an action:
auto-promote (canary rollout), enqueue for human confirmation, or no-op.

Side-effecting hooks (persisting the active model, emitting an approval
request) are injected so this module stays unit-testable. See ARCHITECTURE.md
§13 for the rollout policy (10% canary for 24h, full rollout if error rate
stays stable).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.model_upgrade.scoring import decide_promotion, relative_gain
from master.model_upgrade.types import (
    PromotionDecision,
    PromotionOutcome,
    ShadowEvalResult,
)

log = get_logger(__name__)
tracer = get_tracer(__name__)


@dataclass(frozen=True)
class ReplayQuery:
    """A cached real query + its incumbent response, replayed for shadow eval."""

    query_id: str
    prompt: str
    incumbent_response: str


# score(prompt, response) -> quality in 0.0–1.0 (e.g. judge model or heuristic)
ScoreFn = Callable[[str, str], Awaitable[float]]
# generate(prompt, model_id) -> response text
GenerateFn = Callable[[str, str], Awaitable[str]]


class ShadowEvaluator:
    """
    Runs a candidate model over replayed queries and compares its quality to
    the incumbent. No user-facing requests are issued.
    """

    def __init__(self, generate: GenerateFn, score: ScoreFn) -> None:
        self._generate = generate
        self._score = score

    async def evaluate(
        self,
        candidate_id: str,
        incumbent_id: str,
        queries: list[ReplayQuery],
    ) -> ShadowEvalResult:
        """Score candidate vs incumbent over the replay set."""
        with tracer.start_as_current_span("model_upgrade.shadow.evaluate"):
            if not queries:
                log.warning("model_upgrade.shadow.no_queries", candidate=candidate_id)
                return ShadowEvalResult(
                    candidate_id=candidate_id,
                    incumbent_id=incumbent_id,
                    candidate_score=0.0,
                    incumbent_score=0.0,
                    relative_gain=0.0,
                    sample_count=0,
                )

            cand_total = 0.0
            inc_total = 0.0
            for q in queries:
                candidate_response = await self._generate(q.prompt, candidate_id)
                cand_total += await self._score(q.prompt, candidate_response)
                inc_total += await self._score(q.prompt, q.incumbent_response)

            n = len(queries)
            cand_score = cand_total / n
            inc_score = inc_total / n
            gain = relative_gain(cand_score, inc_score)

            log.info(
                "model_upgrade.shadow.complete",
                candidate=candidate_id,
                incumbent=incumbent_id,
                candidate_score=round(cand_score, 4),
                incumbent_score=round(inc_score, 4),
                relative_gain=round(gain, 4),
                samples=n,
            )
            return ShadowEvalResult(
                candidate_id=candidate_id,
                incumbent_id=incumbent_id,
                candidate_score=cand_score,
                incumbent_score=inc_score,
                relative_gain=gain,
                sample_count=n,
            )


# activate(model_id) — persist the new active model (e.g. write to LiteLLM config / DB)
ActivateFn = Callable[[str], Awaitable[None]]
# request_confirmation(outcome) — enqueue a HitL approval for a borderline gain
RequestConfirmationFn = Callable[[PromotionOutcome], Awaitable[None]]


class ModelPromoter:
    """
    Turns a shadow-eval result into a promotion action.

    Auto-promote triggers the injected ``activate`` hook (the OTA canary
    rollout is driven downstream of activation). Borderline gains are routed to
    ``request_confirmation`` for human-in-the-loop sign-off.
    """

    def __init__(
        self,
        activate: ActivateFn,
        request_confirmation: RequestConfirmationFn,
    ) -> None:
        self._activate = activate
        self._request_confirmation = request_confirmation

    async def maybe_promote(self, result: ShadowEvalResult) -> PromotionOutcome:
        """Decide and act on a shadow-eval result. Returns the outcome taken."""
        outcome = decide_promotion(result)

        if outcome.decision is PromotionDecision.AUTO_PROMOTE:
            await self._activate(outcome.candidate_id)
            log.info(
                "model_upgrade.promote.auto",
                candidate=outcome.candidate_id,
                gain=round(outcome.relative_gain, 4),
            )
        elif outcome.decision is PromotionDecision.NEEDS_CONFIRMATION:
            await self._request_confirmation(outcome)
            log.info(
                "model_upgrade.promote.confirmation_requested",
                candidate=outcome.candidate_id,
                gain=round(outcome.relative_gain, 4),
            )
        else:
            log.info(
                "model_upgrade.promote.skipped",
                candidate=outcome.candidate_id,
                decision=outcome.decision.value,
                reason=outcome.reason,
            )

        return outcome
