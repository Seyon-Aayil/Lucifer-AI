"""
master.model_upgrade.scheduler
==============================
Wires the model-upgrade library (#8) into a scheduled job. On each cycle it
shadow-evaluates the configured candidate models against the current incumbent
(the registry's strong-tier model) over the golden benchmark, and promotes a
candidate that beats the incumbent by the auto-promote margin.

Activation mutates the shared ProviderRegistry's strong model (so live routing
picks it up) and persists the choice to Redis for restart survival.

The `generate(prompt, model_id)` function is injected — production passes a
LiteLLM-backed implementation; tests pass a fake. The job never runs in CI; only
its logic is unit-tested.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from master.core.logging import get_logger
from master.model_upgrade.golden import GOLDEN_TASKS, GoldenTask, substring_score_fn
from master.model_upgrade.promoter import ModelPromoter, ReplayQuery, ShadowEvaluator
from master.model_upgrade.types import PromotionOutcome

log = get_logger(__name__)

# generate(prompt, model_id) -> response text
GenerateFn = Callable[[str, str], Awaitable[str]]

_REDIS_STRONG_KEY = "lucifer:model:strong"


def make_litellm_generate(settings: Any) -> GenerateFn:
    """
    Build a GenerateFn that calls a specific model through the LiteLLM proxy.
    `litellm` is imported lazily so this module stays import-light and unit tests
    never need the dependency.
    """

    async def _generate(prompt: str, model_id: str) -> str:
        import litellm

        resp = await litellm.acompletion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            api_base=settings.litellm_proxy_url,
            api_key=settings.litellm_master_key,
            max_tokens=256,
            temperature=0.0,
        )
        return resp.choices[0].message.content or ""

    return _generate


class ModelUpgradeScheduler:
    """
    Periodic candidate-vs-incumbent shadow evaluation + auto-promotion.

    Reused components:
      - ShadowEvaluator / ModelPromoter / decide_promotion (master.model_upgrade)
      - ProviderRegistry.set_strong_model (activation target)
    """

    def __init__(
        self,
        generate: GenerateFn,
        registry: Any,
        redis: Any = None,
        candidates: list[str] | None = None,
        tasks: list[GoldenTask] | None = None,
    ) -> None:
        self._generate = generate
        self._registry = registry
        self._redis = redis
        self._candidates = candidates or []
        self._tasks = tasks or GOLDEN_TASKS
        self._score = substring_score_fn(self._tasks)

    def attach(self, scheduler: Any, cron: str = "0 2 * * *") -> None:
        """Register the cycle on an AsyncIOScheduler using a crontab expression."""
        scheduler.add_job(
            self.run_cycle,
            trigger=CronTrigger.from_crontab(cron),
            id="model_upgrade",
            replace_existing=True,
        )
        log.info("model_upgrade.scheduler.attached", cron=cron, candidates=len(self._candidates))

    async def run_cycle(self) -> list[PromotionOutcome]:
        """Evaluate every candidate against the incumbent; act on each verdict."""
        incumbent = self._registry.strong_model_id
        evaluator = ShadowEvaluator(self._generate, self._score)
        promoter = ModelPromoter(self._activate, self._request_confirmation)

        outcomes: list[PromotionOutcome] = []
        for candidate in self._candidates:
            if candidate == incumbent:
                continue
            # Build the replay set: the incumbent's own answer is the baseline,
            # so a candidate is only promoted if it scores strictly better.
            queries: list[ReplayQuery] = []
            for t in self._tasks:
                incumbent_answer = await self._generate(t.prompt, incumbent)
                queries.append(ReplayQuery(t.query_id, t.prompt, incumbent_answer))

            result = await evaluator.evaluate(candidate, incumbent, queries)
            outcome = await promoter.maybe_promote(result)
            log.info(
                "model_upgrade.cycle.candidate",
                candidate=candidate,
                incumbent=incumbent,
                decision=str(outcome.decision),
                gain=round(outcome.relative_gain, 4),
            )
            outcomes.append(outcome)

        return outcomes

    async def _activate(self, model_id: str) -> None:
        """Promote: update live routing + persist for restart survival."""
        self._registry.set_strong_model(model_id)
        if self._redis is not None:
            await self._redis.set(_REDIS_STRONG_KEY, model_id)
        log.info("model_upgrade.activated", model_id=model_id)

    async def _request_confirmation(self, outcome: PromotionOutcome) -> None:
        """Borderline gain — log for human review (HitL surface is a follow-up)."""
        log.info(
            "model_upgrade.needs_confirmation",
            candidate=outcome.candidate_id,
            gain=round(outcome.relative_gain, 4),
            reason=outcome.reason,
        )
