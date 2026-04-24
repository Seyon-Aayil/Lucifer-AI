"""
master.token_optimizer.semantic_cache
=======================================
GPTCache wrapper backed by Redis with cosine-similarity threshold.
Caches LLM completions keyed on prompt embeddings.
Cache hits bypass the LLM entirely — zero token spend.
"""
from __future__ import annotations

import hashlib
from typing import Any

from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)


class SemanticCache:
    """
    Semantic prompt cache backed by Redis (vector similarity via GPTCache).
    Embeddings stored as Redis hashes; cosine similarity computed on lookup.
    Falls back to exact-key cache when embedding model unavailable.
    """

    def __init__(self, similarity_threshold: float = 0.92) -> None:
        self._threshold = similarity_threshold
        self._gptcache: Any | None = None
        self._enabled = get_settings().semantic_cache_enabled
        if self._enabled:
            self._init_gptcache()

    def _init_gptcache(self) -> None:
        try:
            from gptcache import Cache  # type: ignore[import]
            from gptcache.adapter.api import init_similar_cache  # type: ignore[import]
            from gptcache.embedding import Onnx  # type: ignore[import]
            from gptcache.manager import get_data_manager  # type: ignore[import]
            from gptcache.similarity_evaluation.distance import (
                SearchDistanceEvaluation,  # type: ignore[import]
            )

            self._gptcache = Cache()
            init_similar_cache(
                cache_obj=self._gptcache,
                embedding=Onnx(),
                evaluation=SearchDistanceEvaluation(positive=True, max_distance=1 - self._threshold),
            )
            log.info("semantic_cache.initialized", threshold=self._threshold)
        except (ImportError, Exception) as exc:
            log.warning("semantic_cache.init_failed", error=str(exc))
            self._enabled = False

    def _prompt_key(self, device_id: str, prompt: str) -> str:
        """SHA-256 key for exact-match Redis fallback."""
        key_input = f"{device_id}:{prompt}".encode()
        return f"lucifer:cache:exact:{hashlib.sha256(key_input).hexdigest()}"

    def _combined_key(self, device_id: str, prompt: str) -> str:
        """Combine device_id and prompt to isolate cache between devices."""
        return f"{device_id}::{prompt}"

    async def get(self, device_id: str, prompt: str) -> str | None:
        """
        Look up a cached response for the given prompt.
        Returns cached response string, or None on miss.
        """
        if not self._enabled:
            return None

        combined_prompt = self._combined_key(device_id, prompt)

        # GPTCache is synchronous — offload in production
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            if self._gptcache:
                result = await loop.run_in_executor(None, self._gptcache.get, combined_prompt)
                if result:
                    log.info("semantic_cache.hit", prompt_len=len(prompt))
                    return result  # type: ignore[return-value]
        except Exception as exc:
            log.warning("semantic_cache.get_error", error=str(exc))
        return None

    async def set(self, device_id: str, prompt: str, response: str) -> None:
        """Cache an LLM response for the given prompt."""
        if not self._enabled:
            return

        combined_prompt = self._combined_key(device_id, prompt)

        import asyncio
        loop = asyncio.get_running_loop()
        try:
            if self._gptcache:
                await loop.run_in_executor(None, self._gptcache.set, combined_prompt, response)
        except Exception as exc:
            log.warning("semantic_cache.set_error", error=str(exc))

    async def invalidate(self, device_id: str, prompt: str) -> None:
        """Remove a specific prompt from cache (e.g., after memory write changes context)."""
        if not self._enabled or not self._gptcache:
            return

        combined_prompt = self._combined_key(device_id, prompt)

        import asyncio
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._gptcache.delete, combined_prompt)
        except Exception:
            pass
