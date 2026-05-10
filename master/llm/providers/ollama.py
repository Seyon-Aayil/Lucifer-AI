"""
master.llm.providers.ollama
============================
Ollama local model adapter.
Routes through LiteLLM proxy or directly to ollama depending on proxy setup.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import litellm

from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import (
    Capability,
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    ProviderHealth,
    ProviderTier,
    StreamChunk,
    TokenUsage,
)

log = get_logger(__name__)
tracer = get_tracer(__name__)


class OllamaProvider(LLMProvider):
    """
    Ollama local provider adapter.
    """

    def __init__(
        self,
        model: str,
        ollama_base_url: str,
        max_context_tokens: int = 8_000,
    ) -> None:
        self._model = model
        self._base_url = ollama_base_url
        self._max_context = max_context_tokens
        # Ollama local is always zero cost
        self._in_cost = 0.0
        self._out_cost = 0.0

    @property
    def provider_id(self) -> str:
        return f"ollama-{self._model}"

    @property
    def tier(self) -> ProviderTier:
        return ProviderTier.DESKTOP

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.TEXT,
                Capability.STREAMING,
                Capability.CODE,
            }
        )

    @property
    def cost_per_input_token(self) -> float:
        return self._in_cost

    @property
    def cost_per_output_token(self) -> float:
        return self._out_cost

    @property
    def max_context_tokens(self) -> int:
        return self._max_context

    def _build_litellm_messages(self, request: CompletionRequest) -> list[dict[str, Any]]:
        messages = []
        for msg in request.messages:
            entry: dict[str, Any] = {"role": msg.role, "content": msg.content}
            messages.append(entry)
        return messages

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        with tracer.start_as_current_span(f"llm.complete.{self.provider_id}"):
            messages = self._build_litellm_messages(request)
            start = time.monotonic()
            resp = await litellm.acompletion(
                model=f"ollama/{self._model}",
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                tools=request.tools,
                tool_choice=request.tool_choice,
                api_base=self._base_url,
            )
            latency_ms = int((time.monotonic() - start) * 1000)

            usage = resp.usage
            token_usage = TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )

            log.info(
                "llm.complete",
                provider=self.provider_id,
                model=self._model,
                input_tokens=token_usage.input_tokens,
                output_tokens=token_usage.output_tokens,
                cost_usd=0.0,
                latency_ms=latency_ms,
            )

            choice = resp.choices[0]
            tool_calls = []
            if choice.message.tool_calls:
                tool_calls = [tc.model_dump() for tc in choice.message.tool_calls]

            return CompletionResponse(
                content=choice.message.content or "",
                model=self._model,
                provider_id=self.provider_id,
                token_usage=token_usage,
                finish_reason=choice.finish_reason or "stop",
                tool_calls=tool_calls,
                raw_response=resp,
            )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        messages = self._build_litellm_messages(request)
        async for chunk in await litellm.acompletion(
            model=f"ollama/{self._model}",
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stream=True,
            api_base=self._base_url,
        ):
            delta = chunk.choices[0].delta.content or ""
            finish = chunk.choices[0].finish_reason
            is_final = finish is not None
            usage = None
            if is_final and hasattr(chunk, "usage") and chunk.usage:
                usage = TokenUsage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            yield StreamChunk(
                delta=delta, is_final=is_final, finish_reason=finish, token_usage=usage
            )

    async def count_tokens(self, text: str) -> int:
        return int(litellm.token_counter(model=f"ollama/{self._model}", text=text))

    async def health_check(self) -> ProviderHealth:
        import httpx

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(self._base_url)
            ok = resp.status_code == 200
            return ProviderHealth(
                provider_id=self.provider_id,
                is_healthy=ok,
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
        except Exception as exc:
            return ProviderHealth(provider_id=self.provider_id, is_healthy=False, error=str(exc))
