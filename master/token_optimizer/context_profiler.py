"""
master.token_optimizer.context_profiler
========================================
BM25 keyword-overlap scoring for context chunks.
Ranks chunks by relevance to the query before compression and trim.
"""

from __future__ import annotations

from master.core.logging import get_logger
from master.token_optimizer.types import ContextChunk

log = get_logger(__name__)


class ContextProfiler:
    """
    Scores context chunks using BM25 keyword overlap.
    No external model required — fast and deterministic.
    """

    def profile(self, query: str, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """
        Score and rank chunks by relevance to query.
        Returns a new list sorted by relevance_score descending.
        """
        query_terms = set(query.lower().split())
        scored: list[ContextChunk] = []

        for chunk in chunks:
            chunk_terms = set(chunk.text.lower().split())
            overlap = len(query_terms & chunk_terms)
            score = (overlap / max(len(query_terms), 1)) + chunk.relevance_score
            scored.append(
                ContextChunk(
                    text=chunk.text,
                    source=chunk.source,
                    relevance_score=score,
                    chunk_id=chunk.chunk_id,
                )
            )

        result = sorted(scored, key=lambda c: c.relevance_score, reverse=True)
        log.debug("context_profiler.scored", chunks=len(result))
        return result
