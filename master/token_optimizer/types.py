"""
master.token_optimizer.types
==============================
Shared data types for the token optimization pipeline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class ContextChunk:
    """A single chunk of context with relevance metadata."""

    text: str
    source: str = ""
    relevance_score: float = 1.0
    chunk_id: str = ""

    def __post_init__(self) -> None:
        if not self.chunk_id:
            self.chunk_id = hashlib.md5(self.text.encode()).hexdigest()[:16]

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class PreparedContext:
    """Result of TokenOptimizer.prepare() — ready for LLM dispatch."""

    chunks: list[ContextChunk]
    cache_hit: bool = False
    cached_response: str | None = None
    total_tokens: int = 0
    stages_applied: list[str] = field(default_factory=list)

    def as_string(self) -> str:
        """Flatten all chunks into a single context string."""
        return "\n\n".join(c.text for c in self.chunks)
