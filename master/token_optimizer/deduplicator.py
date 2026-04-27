"""
master.token_optimizer.deduplicator
====================================
SimHash near-duplicate removal for context chunks.
Falls back to MD5 exact-dedup when the simhash package is unavailable.
"""
from __future__ import annotations

from master.core.logging import get_logger
from master.token_optimizer.types import ContextChunk

log = get_logger(__name__)


class SemanticDeduplicator:
    """
    Removes near-duplicate chunks using SimHash (Hamming distance).
    Requires the `simhash` package; falls back to MD5 exact-dedup if absent.
    """

    def __init__(self, hamming_threshold: int = 4) -> None:
        self._threshold = hamming_threshold

    def deduplicate(self, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """
        Filter out near-duplicate chunks. Returns the deduplicated list
        preserving original order (first occurrence wins).
        """
        try:
            from simhash import Simhash, SimhashIndex  # type: ignore[import]

            unique: list[ContextChunk] = []
            index = SimhashIndex([], k=self._threshold)

            for chunk in chunks:
                sh = Simhash(chunk.text)
                if not index.get_near_dups(sh):
                    unique.append(chunk)
                    index.add(chunk.chunk_id, sh)

            dropped = len(chunks) - len(unique)
            if dropped:
                log.debug("deduplicator.dropped", dropped=dropped, strategy="simhash")
            return unique

        except (ImportError, Exception):
            seen: set[str] = set()
            unique_chunks: list[ContextChunk] = []
            for chunk in chunks:
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    unique_chunks.append(chunk)

            dropped = len(chunks) - len(unique_chunks)
            if dropped:
                log.debug("deduplicator.dropped", dropped=dropped, strategy="md5_fallback")
            return unique_chunks
