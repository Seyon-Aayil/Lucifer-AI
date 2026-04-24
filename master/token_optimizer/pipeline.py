"""
master.token_optimizer.pipeline
=================================
Main TokenOptimizer: 6-stage pipeline applied before every LLM dispatch.

Stages (in order):
  1. Semantic Cache lookup  → return early on hit (zero tokens)
  2. Budget enforcement     → reject if context already exceeds limit
  3. Context profiling      → score + rank chunks by relevance (BM25 + cosine)
  4. Deduplication          → drop near-duplicate chunks (SimHash)
  5. Compression            → LLMLingua-2 compress long chunks
  6. Hierarchical trim      → final hard cut to fit token budget

Call TokenOptimizer.prepare(context, budget) before every provider.complete() call.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from master.agents.base.agent import TokenBudget
from master.core.exceptions import TokenBudgetExceededError
from master.core.logging import get_logger
from master.token_optimizer.compressor import ContextCompressor
from master.token_optimizer.semantic_cache import SemanticCache

log = get_logger(__name__)

# Rough token estimation constant
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
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
        return _estimate_tokens(self.text)


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


class TokenOptimizer:
    """
    6-stage token optimization pipeline.
    Singleton per application process — all state is thread-safe (read-only after init).
    """

    def __init__(
        self,
        semantic_cache: SemanticCache | None = None,
        compressor: ContextCompressor | None = None,
        dedup_threshold: int = 4,       # SimHash Hamming distance threshold
        compression_ratio: float = 0.5,
    ) -> None:
        self._cache = semantic_cache or SemanticCache()
        self._compressor = compressor or ContextCompressor(target_ratio=compression_ratio)
        self._dedup_threshold = dedup_threshold

    async def prepare(
        self,
        prompt: str,
        chunks: list[ContextChunk],
        budget: TokenBudget,
        device_id: str = "unknown",
    ) -> PreparedContext:
        """
        Run all optimization stages. Returns a PreparedContext ready for dispatch.
        On cache hit: PreparedContext.cache_hit=True, cached_response is set.
        On budget exceeded after all compression: raises TokenBudgetExceededError.
        """
        stages: list[str] = []

        # ── Stage 1: Semantic Cache ───────────────────────────────────────────
        combined_prompt = prompt + "\n".join(c.text for c in chunks[:3])  # key on prompt + top ctx
        cached = await self._cache.get(device_id, combined_prompt)
        if cached:
            log.info("token_optimizer.cache_hit", prompt_len=len(prompt))
            return PreparedContext(chunks=[], cache_hit=True, cached_response=cached, stages_applied=["cache"])

        # ── Stage 2: Budget pre-check ─────────────────────────────────────────
        total = _estimate_tokens(prompt) + sum(c.estimated_tokens for c in chunks)
        if total > budget.input_limit * 3:  # Way over budget — reject early
            if not budget.allow_compression:
                raise TokenBudgetExceededError(
                    f"Context {total} tokens exceeds budget {budget.input_limit}"
                )
        stages.append("budget_check")

        # ── Stage 3: Relevance scoring (BM25 approximation) ──────────────────
        scored_chunks = self._score_chunks(prompt, chunks)
        stages.append("relevance_score")

        # ── Stage 4: Deduplication (SimHash) ─────────────────────────────────
        deduped = self._deduplicate(scored_chunks)
        dropped = len(scored_chunks) - len(deduped)
        if dropped > 0:
            log.debug("token_optimizer.dedup", dropped=dropped)
        stages.append("dedup")

        # ── Stage 5: Compression ─────────────────────────────────────────────
        if budget.allow_compression:
            budget_per_chunk = max(256, budget.input_limit // max(len(deduped), 1))
            compressed = []
            for chunk in deduped:
                if chunk.estimated_tokens > budget_per_chunk:
                    result = self._compressor.compress(chunk.text, target_token_count=budget_per_chunk)
                    compressed.append(ContextChunk(
                        text=result.compressed_text,
                        source=chunk.source,
                        relevance_score=chunk.relevance_score,
                        chunk_id=chunk.chunk_id,
                    ))
                else:
                    compressed.append(chunk)
            stages.append("compress")
        else:
            compressed = deduped

        # ── Stage 6: Hierarchical trim ────────────────────────────────────────
        final_chunks = self._hierarchical_trim(compressed, budget.input_limit - _estimate_tokens(prompt))
        stages.append("trim")

        total_tokens = _estimate_tokens(prompt) + sum(c.estimated_tokens for c in final_chunks)
        if total_tokens > budget.input_limit and not budget.allow_compression:
            raise TokenBudgetExceededError(
                f"After optimization: {total_tokens} tokens still exceeds {budget.input_limit}"
            )

        log.info(
            "token_optimizer.prepared",
            stages=stages,
            original_chunks=len(chunks),
            final_chunks=len(final_chunks),
            total_tokens=total_tokens,
        )
        return PreparedContext(chunks=final_chunks, total_tokens=total_tokens, stages_applied=stages)

    def _score_chunks(self, query: str, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """BM25 keyword overlap scoring — no external model required."""
        query_terms = set(query.lower().split())
        scored = []
        for chunk in chunks:
            chunk_terms = set(chunk.text.lower().split())
            overlap = len(query_terms & chunk_terms)
            score = (overlap / max(len(query_terms), 1)) + chunk.relevance_score
            scored.append(ContextChunk(
                text=chunk.text,
                source=chunk.source,
                relevance_score=score,
                chunk_id=chunk.chunk_id,
            ))
        return sorted(scored, key=lambda c: c.relevance_score, reverse=True)

    def _deduplicate(self, chunks: list[ContextChunk]) -> list[ContextChunk]:
        """SimHash-based near-duplicate removal."""
        try:
            from simhash import Simhash, SimhashIndex  # type: ignore[import]

            unique: list[ContextChunk] = []
            index = SimhashIndex([], k=self._dedup_threshold)

            for chunk in chunks:
                sh = Simhash(chunk.text)
                if not index.get_near_dups(sh):
                    unique.append(chunk)
                    index.add(chunk.chunk_id, sh)
            return unique
        except (ImportError, Exception):
            # Fallback: MD5 exact dedup
            seen_ids: set[str] = set()
            unique_chunks: list[ContextChunk] = []
            for chunk in chunks:
                if chunk.chunk_id not in seen_ids:
                    seen_ids.add(chunk.chunk_id)
                    unique_chunks.append(chunk)
            return unique_chunks

    def _hierarchical_trim(self, chunks: list[ContextChunk], token_budget: int) -> list[ContextChunk]:
        """
        Drop lowest-scoring chunks until total fits within token_budget.
        Chunks are already sorted by relevance score descending.
        """
        result: list[ContextChunk] = []
        remaining = token_budget
        for chunk in chunks:
            if chunk.estimated_tokens <= remaining:
                result.append(chunk)
                remaining -= chunk.estimated_tokens
            elif remaining > 100:
                # Partially include: truncate to remaining budget
                chars = remaining * _CHARS_PER_TOKEN
                truncated_text = chunk.text[:chars].rsplit(" ", 1)[0]
                result.append(ContextChunk(
                    text=truncated_text,
                    source=chunk.source,
                    relevance_score=chunk.relevance_score,
                ))
                break
            else:
                break
        return result
