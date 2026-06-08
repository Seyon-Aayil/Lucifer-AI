"""Unit tests for the model-upgrade scheduler + golden benchmark + activation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from master.llm.registry import ProviderRegistry, reset_shared_registry, shared_registry
from master.model_upgrade.golden import GoldenTask, substring_score_fn
from master.model_upgrade.judge import parse_judge_score
from master.model_upgrade.replay import make_replay_loader, record_query
from master.model_upgrade.scheduler import (
    ModelUpgradeScheduler,
    apply_persisted_strong_model,
)
from master.model_upgrade.types import PromotionDecision


def _pool(*, rows=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows if rows is not None else [])
    conn.execute = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


_TASKS = [
    GoldenTask("a", "Pa", "xa"),
    GoldenTask("b", "Pb", "xb"),
]


def _registry(strong: str = "incumbent-1") -> MagicMock:
    reg = MagicMock()
    reg.strong_model_id = strong
    reg.set_strong_model = MagicMock()
    return reg


def _make_generate(candidate: str):
    answers = {"Pa": "xa", "Pb": "xb"}

    async def gen(prompt: str, model_id: str) -> str:
        if model_id == candidate:
            return answers[prompt]  # candidate: always correct
        # incumbent: correct on Pa only -> score 0.5
        return answers[prompt] if prompt == "Pa" else "nope"

    return gen


class TestSubstringScore:
    async def test_hit_and_miss(self):
        score = substring_score_fn(_TASKS)
        assert await score("Pa", "the answer is XA!") == 1.0
        assert await score("Pa", "wrong") == 0.0
        assert await score("unknown-prompt", "xa") == 0.0


class TestJudgeScore:
    def test_parses_and_normalises(self):
        assert parse_judge_score("8") == 0.8
        assert parse_judge_score("Score: 10") == 1.0
        assert parse_judge_score("0") == 0.0

    def test_clamps_and_handles_garbage(self):
        assert parse_judge_score("12") == 1.0  # clamp >10
        assert parse_judge_score("-3") == 0.0  # clamp <0
        assert parse_judge_score("no number here") == 0.0
        assert parse_judge_score("") == 0.0


class TestInjectedScore:
    async def test_uses_injected_scorer(self):
        reg = _registry("incumbent-1")

        # injected scorer: candidate model scores 1.0, incumbent answers score 0.5
        async def score(prompt: str, response: str) -> float:
            return 1.0 if response == "GREAT" else 0.5

        async def gen(prompt: str, model_id: str) -> str:
            return "GREAT" if model_id == "cand-1" else "meh"

        sched = ModelUpgradeScheduler(
            generate=gen,
            registry=reg,
            redis=AsyncMock(),
            candidates=["cand-1"],
            tasks=_TASKS,
            score=score,
        )
        outcomes = await sched.run_cycle()
        assert outcomes[0].decision is PromotionDecision.AUTO_PROMOTE
        reg.set_strong_model.assert_called_once_with("cand-1")


class TestReplay:
    async def test_record_query_inserts(self):
        pool, conn = _pool()
        await record_query(pool, "prompt", "response", "coding")
        conn.execute.assert_awaited_once()

    async def test_record_query_skips_empty(self):
        pool, conn = _pool()
        await record_query(pool, "", "response")
        conn.execute.assert_not_awaited()

    async def test_replay_loader_maps_rows(self):
        rows = [
            {"id": "r1", "prompt": "Pa", "response": "ans-a"},
            {"id": "r2", "prompt": "Pb", "response": "ans-b"},
        ]
        pool, _ = _pool(rows=rows)
        loader = make_replay_loader(pool, limit=10)
        queries = await loader("incumbent")
        assert [q.query_id for q in queries] == ["r1", "r2"]
        assert queries[0].incumbent_response == "ans-a"

    async def test_replay_loader_falls_back_to_golden_when_empty(self):
        pool, _ = _pool(rows=[])
        loader = make_replay_loader(pool, tasks=_TASKS)
        queries = await loader("incumbent")
        # golden fallback: incumbent_response is the golden expected substring
        assert {q.prompt for q in queries} == {"Pa", "Pb"}
        assert queries[0].incumbent_response in {"xa", "xb"}

    async def test_scheduler_uses_injected_loader(self):
        reg = _registry("incumbent-1")
        from master.model_upgrade.promoter import ReplayQuery

        async def loader(_incumbent: str):
            return [ReplayQuery("q1", "Pa", "incumbent-ans")]

        async def gen(prompt: str, model_id: str) -> str:
            return "xa" if model_id == "cand-1" else "incumbent-ans"

        # candidate answers correctly (xa in golden), incumbent baseline does not
        sched = ModelUpgradeScheduler(
            generate=gen,
            registry=reg,
            redis=AsyncMock(),
            candidates=["cand-1"],
            tasks=_TASKS,
            queries_loader=loader,
        )
        outcomes = await sched.run_cycle()
        assert len(outcomes) == 1


class TestRunCycle:
    async def test_promotes_better_candidate(self):
        reg = _registry("incumbent-1")
        redis = AsyncMock()
        sched = ModelUpgradeScheduler(
            generate=_make_generate("cand-1"),
            registry=reg,
            redis=redis,
            candidates=["cand-1"],
            tasks=_TASKS,
        )
        outcomes = await sched.run_cycle()

        assert len(outcomes) == 1
        assert outcomes[0].decision is PromotionDecision.AUTO_PROMOTE
        reg.set_strong_model.assert_called_once_with("cand-1")
        redis.set.assert_awaited_once()
        key, val = redis.set.await_args.args
        assert val == "cand-1"

    async def test_rejects_non_better_candidate(self):
        reg = _registry("incumbent-1")

        # candidate scores the same as incumbent (both correct on Pa only).
        async def gen(prompt: str, model_id: str) -> str:
            return "xa" if prompt == "Pa" else "nope"

        sched = ModelUpgradeScheduler(
            generate=gen, registry=reg, redis=AsyncMock(), candidates=["cand-1"], tasks=_TASKS
        )
        outcomes = await sched.run_cycle()

        assert outcomes[0].decision is not PromotionDecision.AUTO_PROMOTE
        reg.set_strong_model.assert_not_called()

    async def test_skips_candidate_equal_to_incumbent(self):
        reg = _registry("cand-1")
        sched = ModelUpgradeScheduler(
            generate=_make_generate("cand-1"),
            registry=reg,
            redis=AsyncMock(),
            candidates=["cand-1"],
            tasks=_TASKS,
        )
        outcomes = await sched.run_cycle()
        assert outcomes == []
        reg.set_strong_model.assert_not_called()

    def test_attach_registers_cron_job(self):
        sched = ModelUpgradeScheduler(generate=AsyncMock(), registry=_registry())
        scheduler = MagicMock()
        sched.attach(scheduler, cron="0 3 * * *")
        scheduler.add_job.assert_called_once()
        assert scheduler.add_job.call_args.kwargs["id"] == "model_upgrade"


class TestRegistryActivation:
    def test_set_strong_model_updates_routing_target(self):
        reg = ProviderRegistry(providers=[], strong_model_id="old")
        assert reg.strong_model_id == "old"
        reg.set_strong_model("new")
        assert reg.strong_model_id == "new"

    def test_shared_registry_is_singleton(self):
        reset_shared_registry()
        a = shared_registry()
        b = shared_registry()
        assert a is b
        reset_shared_registry()
        assert shared_registry() is not a
        reset_shared_registry()


class TestPersistedOverride:
    async def test_applies_override_from_redis(self):
        reg = _registry("incumbent-1")
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"promoted-2")
        applied = await apply_persisted_strong_model(reg, redis)
        assert applied == "promoted-2"
        reg.set_strong_model.assert_called_once_with("promoted-2")

    async def test_no_override_is_noop(self):
        reg = _registry("incumbent-1")
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        assert await apply_persisted_strong_model(reg, redis) is None
        reg.set_strong_model.assert_not_called()

    async def test_matching_override_is_noop(self):
        reg = _registry("incumbent-1")
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"incumbent-1")
        assert await apply_persisted_strong_model(reg, redis) is None
        reg.set_strong_model.assert_not_called()

    async def test_redis_failure_is_swallowed(self):
        reg = _registry("incumbent-1")
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=RuntimeError("down"))
        assert await apply_persisted_strong_model(reg, redis) is None
        reg.set_strong_model.assert_not_called()
