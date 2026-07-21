"""
master.llm.interfaces
======================
Abstract interfaces for LLM providers and related data types.
ALL provider implementations must satisfy LLMProvider exactly.
This indirection is what makes Lucifer LLM-agnostic.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# ── Enums & Value Types ────────────────────────────────────────────────────────


class ProviderTier(enum.StrEnum):
    """Which deployment tier a provider belongs to."""

    MASTER = "master"  # Cloud APIs or self-hosted vLLM on master server
    DESKTOP = "desktop"  # Ollama / MLX on local machine
    MOBILE = "mobile"  # Core ML / Foundation Models / ONNX


class Capability(enum.StrEnum):
    """Capabilities that a provider may or may not support."""

    TEXT = "text"
    VISION = "vision"
    FUNCTION_CALLING = "function_calling"
    STREAMING = "streaming"
    EMBEDDING = "embedding"
    CODE = "code"


# Anthropic prompt-caching economics, as multiples of the base input-token rate:
# a cache read costs 0.1×, a cache write (creation) costs 1.25×.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption from a single LLM completion call."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """
        Fraction of input tokens served from cache (cache_read / input_tokens).

        LiteLLM normalises input_tokens to include cached tokens, so this is a
        genuine 0.0–1.0 hit-rate. 0.0 when there are no input tokens.
        """
        if self.input_tokens <= 0:
            return 0.0
        return self.cache_read_tokens / self.input_tokens

    def cost(
        self,
        cost_per_input: float,
        cost_per_output: float,
        cache_read_multiplier: float = CACHE_READ_MULTIPLIER,
        cache_write_multiplier: float = CACHE_WRITE_MULTIPLIER,
    ) -> float:
        """
        Compute estimated cost in USD, pricing all four token classes.

        LiteLLM normalises Anthropic usage so ``input_tokens`` (prompt_tokens)
        INCLUDES both cache-read and cache-write tokens. To avoid double-charging,
        the cached tokens are billed at their own rates and only the remainder is
        billed at the full input rate:

            full = input_tokens − cache_read_tokens − cache_write_tokens
            cost = full           × cost_per_input
                 + cache_read     × cost_per_input × 0.1
                 + cache_write    × cost_per_input × 1.25
                 + output_tokens  × cost_per_output

        With zero cache tokens this reduces to the plain input/output pricing, so
        existing callers are unaffected.
        """
        full_rate_input = max(
            0, self.input_tokens - self.cache_read_tokens - self.cache_write_tokens
        )
        return (
            full_rate_input * cost_per_input
            + self.cache_read_tokens * cost_per_input * cache_read_multiplier
            + self.cache_write_tokens * cost_per_input * cache_write_multiplier
            + self.output_tokens * cost_per_output
        )


@dataclass
class Message:
    """A single turn in a conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list[Any]  # str for text; list for multimodal (vision)
    name: str | None = None  # Tool name (for role="tool")
    tool_call_id: str | None = None


@dataclass
class CompletionRequest:
    """
    Unified completion request — provider-agnostic.
    Adapters translate this to their SDK's format.
    """

    messages: list[Message]
    model: str  # Provider-specific model ID
    max_tokens: int = 4096
    temperature: float = 0.7
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    # Reasoning depth dial (low | medium | high | xhigh). Mapped from RouteLLM
    # complexity by the registry; only honoured by effort-capable models.
    effort: str | None = None
    # JSON-schema dict to constrain the response (structured outputs). Callers
    # may pass SomeModel.model_json_schema(). Only honoured by capable models.
    response_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResponse:
    """Unified completion response from any provider."""

    content: str
    model: str
    provider_id: str
    token_usage: TokenUsage
    finish_reason: str  # "stop" | "length" | "tool_calls" | "error"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_response: Any = None  # Original SDK response (for debugging)


@dataclass(frozen=True)
class ProviderSelection:
    """
    Result of ProviderRegistry.select: the chosen provider plus the per-request
    reasoning effort derived from the RouteLLM complexity score (None when the
    weak tier is chosen or effort does not apply).
    """

    provider: LLMProvider
    effort: str | None = None


@dataclass
class StreamChunk:
    """A single streamed delta from a provider."""

    delta: str
    is_final: bool = False
    finish_reason: str | None = None
    token_usage: TokenUsage | None = None  # Populated on is_final=True


@dataclass
class ProviderHealth:
    """Health snapshot for a single provider."""

    provider_id: str
    is_healthy: bool
    latency_ms: float | None = None
    error: str | None = None
    circuit_open: bool = False


# ── Abstract Interface ────────────────────────────────────────────────────────


class LLMProvider(ABC):
    """
    Abstract base for all LLM provider adapters.
    Implementations live in master/llm/providers/.
    Instantiated by ProviderRegistry — never created directly in agent code.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique identifier e.g. 'anthropic-claude-opus-4'."""

    @property
    @abstractmethod
    def tier(self) -> ProviderTier:
        """Which deployment tier this provider belongs to."""

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Set of capabilities this provider supports."""

    @property
    @abstractmethod
    def cost_per_input_token(self) -> float:
        """USD cost per input token. 0.0 for local models."""

    @property
    @abstractmethod
    def cost_per_output_token(self) -> float:
        """USD cost per output token. 0.0 for local models."""

    @property
    @abstractmethod
    def max_context_tokens(self) -> int:
        """Maximum context window in tokens."""

    @property
    def cache_read_multiplier(self) -> float:
        """
        Cache-read price as a multiple of the base input rate. Defaults to
        Anthropic's economics (0.1×); providers whose caching is priced
        differently (e.g. Gemini ≈ 0.25×) override this.
        """
        return CACHE_READ_MULTIPLIER

    @property
    def cache_write_multiplier(self) -> float:
        """
        Cache-write (creation) price as a multiple of the base input rate.
        Defaults to Anthropic's 1.25×. Providers without a per-token creation
        premium (e.g. Gemini, whose cache cost is storage-over-time) override
        this to 1.0.
        """
        return CACHE_WRITE_MULTIPLIER

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """
        Execute a completion request and return the full response.
        Must respect request.max_tokens — never exceed it.
        """

    @abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Yield streamed chunks. Last chunk has is_final=True with token_usage."""

    @abstractmethod
    async def count_tokens(self, text: str) -> int:
        """Estimate token count for the given text under this provider's tokeniser."""

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Check provider reachability and latency. Should not raise."""

    def supports(self, *caps: Capability) -> bool:
        """Return True if this provider supports all requested capabilities."""
        return all(c in self.capabilities for c in caps)
