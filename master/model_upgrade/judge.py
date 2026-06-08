"""
master.model_upgrade.judge
==========================
LLM-as-judge scorer for shadow evaluation. Instead of exact-substring matching
against a golden answer, a judge model rates the quality of a candidate response
on a 0-10 scale, normalised to 0.0-1.0.

`litellm` is imported lazily inside the closure so this module stays import-light
and the pure parser (`parse_judge_score`) is unit-testable without the dependency.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from master.core.logging import get_logger

log = get_logger(__name__)

# score(prompt, response) -> 0.0..1.0
ScoreFn = Callable[[str, str], Awaitable[float]]

_JUDGE_TEMPLATE = (
    "You are grading the quality of an assistant RESPONSE to a PROMPT. "
    "Rate it on a 0-10 integer scale where 10 is excellent (correct, relevant, "
    "helpful) and 0 is useless or wrong. Reply with ONLY the number.\n\n"
    "PROMPT:\n{prompt}\n\nRESPONSE:\n{response}\n\nScore:"
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_judge_score(text: str) -> float:
    """
    Extract the first number from the judge's reply and normalise 0-10 → 0.0-1.0.
    Non-numeric or empty replies score 0.0; values are clamped to [0, 1].
    """
    match = _NUM_RE.search(text or "")
    if not match:
        return 0.0
    try:
        raw = float(match.group())
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, raw / 10.0))


def make_llm_judge_score(settings: Any, judge_model: str) -> ScoreFn:
    """Build a ScoreFn that grades responses with `judge_model` via LiteLLM."""

    async def _score(prompt: str, response: str) -> float:
        import litellm

        try:
            resp = await litellm.acompletion(
                model=judge_model,
                messages=[
                    {
                        "role": "user",
                        "content": _JUDGE_TEMPLATE.format(prompt=prompt, response=response),
                    }
                ],
                api_base=settings.litellm_proxy_url,
                api_key=settings.litellm_master_key,
                max_tokens=8,
                temperature=0.0,
            )
            return parse_judge_score(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001 — a judge failure must not abort the cycle
            log.warning("model_upgrade.judge_failed", model=judge_model, error=str(exc))
            return 0.0

    return _score
