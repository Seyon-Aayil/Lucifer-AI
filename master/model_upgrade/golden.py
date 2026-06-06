"""
master.model_upgrade.golden
===========================
A small built-in golden benchmark used by the model-upgrade scheduler as a
starter evaluation set. Each task pairs a deterministic prompt with a substring
the answer must contain (case-insensitive). Scoring is exact-substring — cheap,
provider-agnostic, and good enough to catch a candidate that regresses on basic
competence.

This is intentionally minimal: expand it (or swap in sampled real-traffic replay
+ an LLM-judge scorer) as the model-upgrade program matures.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenTask:
    """A benchmark prompt + a substring a correct answer must contain."""

    query_id: str
    prompt: str
    expected: str  # case-insensitive substring


GOLDEN_TASKS: list[GoldenTask] = [
    GoldenTask("capital-fr", "What is the capital of France? Answer in one word.", "paris"),
    GoldenTask("math-add", "What is 2 + 2? Reply with only the number.", "4"),
    GoldenTask(
        "antonym-hot", "Give the single-word antonym of 'hot'. Reply with one word.", "cold"
    ),
    GoldenTask(
        "json-key",
        'Return a JSON object with a single key "ok" set to true. Reply with only JSON.',
        '"ok"',
    ),
    GoldenTask(
        "spell-count",
        "How many letters are in the word 'lucifer'? Reply with only the number.",
        "7",
    ),
    GoldenTask(
        "lang-id",
        "What language is 'bonjour'? Answer with one word.",
        "french",
    ),
]

# score(prompt, response) -> 0.0 | 1.0
ScoreFn = Callable[[str, str], Awaitable[float]]


def substring_score_fn(tasks: list[GoldenTask]) -> ScoreFn:
    """Build a ScoreFn that returns 1.0 iff the task's expected substring is present."""
    expected = {t.prompt: t.expected.lower() for t in tasks}

    async def _score(prompt: str, response: str) -> float:
        want = expected.get(prompt)
        if not want:
            return 0.0
        return 1.0 if want in response.lower() else 0.0

    return _score
