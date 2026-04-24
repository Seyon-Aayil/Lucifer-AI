"""
master.token_optimizer.compressor
===================================
LLMLingua-2 context compressor.
Compresses long documents and RAG chunks to fit within token budgets.
Falls back to truncation if the model is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass

from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class CompressedResult:
    """Output from the compressor pipeline."""
    compressed_text: str
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float


class ContextCompressor:
    """
    LLMLingua-2 context compressor.
    Used in the token optimizer pipeline to reduce long documents.
    Thread-safe — model loaded once at construction time.
    """

    def __init__(self, target_ratio: float = 0.5) -> None:
        """
        Args:
            target_ratio: Target size as fraction of original (0.5 = 50% compression).
        """
        self._target_ratio = target_ratio
        self._llm_lingua: object | None = None
        self._enabled = get_settings().token_optimizer_enabled
        if self._enabled:
            self._load_model()

    def _load_model(self) -> None:
        try:
            from llmlingua import PromptCompressor  # type: ignore[import]

            self._llm_lingua = PromptCompressor(
                model_name=get_settings().llmlingua_model,
                use_llmlingua2=True,
                device_map="cpu",
            )
            log.info("compressor.llmlingua2.loaded", model=get_settings().llmlingua_model)
        except (ImportError, Exception) as exc:
            log.warning("compressor.llmlingua2.unavailable", error=str(exc))
            self._enabled = False

    def compress(self, text: str, target_token_count: int | None = None) -> CompressedResult:
        """
        Compress text using LLMLingua-2.
        Falls back to smart truncation if model unavailable.

        Args:
            text: Input text to compress.
            target_token_count: Hard token count target. Overrides target_ratio if set.
        """
        if not self._enabled or self._llm_lingua is None:
            return self._truncate_fallback(text, target_token_count)

        try:
            # Estimate input tokens (rough: 1 token ≈ 4 chars)
            original_tokens = len(text) // 4
            ratio = self._target_ratio
            if target_token_count and original_tokens > 0:
                ratio = min(target_token_count / original_tokens, 1.0)

            result = self._llm_lingua.compress_prompt(  # type: ignore[union-attr]
                context=[text],
                ratio=ratio,
                force_tokens=["\\n", ".", "!", "?"],
                drop_consecutive=True,
            )
            compressed = result["compressed_prompt"]
            compressed_tokens = len(compressed) // 4

            log.info(
                "compressor.compressed",
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                ratio=round(compressed_tokens / max(original_tokens, 1), 3),
            )

            return CompressedResult(
                compressed_text=compressed,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                compression_ratio=compressed_tokens / max(original_tokens, 1),
            )
        except Exception as exc:
            log.warning("compressor.failed", error=str(exc))
            return self._truncate_fallback(text, target_token_count)

    def _truncate_fallback(self, text: str, target_token_count: int | None) -> CompressedResult:
        """Fallback: truncate at word boundaries to approximate target."""
        original_tokens = len(text) // 4
        if target_token_count and original_tokens > target_token_count:
            chars = target_token_count * 4
            truncated = text[:chars].rsplit(" ", 1)[0] + " [truncated]"
        else:
            truncated = text
        compressed_tokens = len(truncated) // 4
        return CompressedResult(
            compressed_text=truncated,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compressed_tokens / max(original_tokens, 1),
        )
