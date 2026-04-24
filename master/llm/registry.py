"""
master.llm.registry
====================
ProviderRegistry: runtime registry with RouteLLM-aware provider selection.
RouteLLM classifies query complexity → selects weak/strong tier.
LiteLLM proxy executes the call and enforces budget caps.
Circuit breaker state is tracked per provider.

Usage:
    registry = ProviderRegistry.from_settings()
    provider = await registry.select(
        query="...", tier=ProviderTier.MASTER,
        required_capabilities={Capability.TEXT},
        agent_id="coding-agent", max_budget_usd=0.05
    )
    response = await provider.complete(req)
"""
from __future__ import annotations

import asyncio
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from master.core.config import get_settings
from master.core.exceptions import NoSuitableProviderError, ProviderError
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.circuit_breaker import CircuitBreaker
from master.llm.interfaces import (
    Capability,
    CompletionRequest,
    CompletionResponse,
    LLMProvider,
    ProviderTier,
)

log = get_logger(__name__)
tracer = get_tracer(__name__)


class ProviderRegistry:
    """
    Registry for all available LLM providers.
    Thread-safe via per-provider circuit breakers.
    RouteLLM complexity scoring routes to weak/strong model.
    """

    def __init__(
        self,
        providers: list[LLMProvider],
        routellm_threshold: float = 0.5,
        strong_model_id: str = "anthropic-claude-opus-4",
        weak_model_id: str = "anthropic-claude-haiku-4",
    ) -> None:
        self._providers: dict[str, LLMProvider] = {p.provider_id: p for p in providers}
        self._breakers: dict[str, CircuitBreaker] = {
            p.provider_id: CircuitBreaker(p.provider_id) for p in providers
        }
        self._routellm_threshold = routellm_threshold
        self._strong_model_id = strong_model_id
        self._weak_model_id = weak_model_id
        # Lazy-loaded RouteLLM controller
        self._routellm: Any | None = None

    @classmethod
    def from_settings(cls, extra_providers: list[LLMProvider] | None = None) -> ProviderRegistry:
        """
        Factory: build a ProviderRegistry from application settings.
        Registers: Anthropic (opus, sonnet, haiku), OpenAI (gpt-4o, mini),
                   Google (flash, pro), Ollama (llama3).
        """
        from master.llm.providers.anthropic import AnthropicProvider

        settings = get_settings()
        providers: list[LLMProvider] = []

        if settings.anthropic_api_key:
            for model in ("claude-opus-4", "claude-sonnet-4-5", "claude-haiku-4"):
                providers.append(
                    AnthropicProvider(
                        model=model,
                        litellm_proxy_url=settings.litellm_proxy_url,
                        litellm_api_key=settings.litellm_master_key,
                    )
                )

        # OpenAI, Google, Ollama adapters follow the same pattern (Phase 1 TODO)
        # Add them here as their adapters are implemented.
        from master.llm.providers.google import GoogleProvider
        from master.llm.providers.ollama import OllamaProvider
        from master.llm.providers.openai import OpenAIProvider

        if settings.openai_api_key:
            for model in ("gpt-4o", "gpt-4o-mini"):
                providers.append(
                    OpenAIProvider(
                        model=model,
                        litellm_proxy_url=settings.litellm_proxy_url,
                        litellm_api_key=settings.litellm_master_key,
                    )
                )

        if settings.google_api_key:
            for model in ("gemini-1.5-pro", "gemini-1.5-flash"):
                providers.append(
                    GoogleProvider(
                        model=model,
                        litellm_proxy_url=settings.litellm_proxy_url,
                        litellm_api_key=settings.litellm_master_key,
                    )
                )

        if settings.ollama_base_url:
            providers.append(
                OllamaProvider(
                    model="llama3",
                    ollama_base_url=settings.ollama_base_url,
                )
            )

        if extra_providers:
            providers.extend(extra_providers)

        return cls(
            providers=providers,
            routellm_threshold=settings.routellm_threshold,
            strong_model_id=f"anthropic-{settings.routellm_strong_model.split('/')[-1]}",
            weak_model_id=f"anthropic-{settings.routellm_weak_model.split('/')[-1]}",
        )

    async def _routellm_score(self, query: str) -> float:
        """
        Score query complexity using RouteLLM controller.
        Returns 0.0–1.0. Above threshold → strong model.
        Falls back to 0.5 (ambiguous) if RouteLLM unavailable.
        """
        try:
            if self._routellm is None:
                from routellm.controller import Controller  # type: ignore[import]

                self._routellm = Controller(
                    routers=["mf"],  # matrix-factorisation router (fast)
                    strong_model=self._strong_model_id,
                    weak_model=self._weak_model_id,
                )
            # RouteLLM returns a complexity score directly
            score: float = await asyncio.get_event_loop().run_in_executor(
                None, self._routellm.score, query
            )
            return score
        except Exception as exc:
            log.warning("routellm.score.failed", error=str(exc))
            return 0.5  # Default to ambiguous

    async def select(
        self,
        query: str,
        tier: ProviderTier = ProviderTier.MASTER,
        required_capabilities: set[Capability] | None = None,
        agent_id: str = "unknown",
        max_budget_usd: float = 1.0,
    ) -> LLMProvider:
        """
        Select the most appropriate provider for a request.
        1. RouteLLM scores complexity → selects preferred model.
        2. Filters candidates by tier + capabilities + circuit state + budget.
        3. Falls back through chain if preferred is unavailable.
        Raises: NoSuitableProviderError if no provider can serve the request.
        """
        with tracer.start_as_current_span("llm.registry.select"):
            caps = required_capabilities or {Capability.TEXT}

            # RouteLLM: pick preferred provider ID
            score = await self._routellm_score(query)
            preferred_id = self._strong_model_id if score >= self._routellm_threshold else self._weak_model_id
            log.debug(
                "routellm.route",
                score=round(score, 3),
                preferred=preferred_id,
                agent=agent_id,
            )

            # Build candidate list: preferred first, then others by tier
            candidates = self._rank_candidates(preferred_id, tier, caps, max_budget_usd)

            for provider in candidates:
                breaker = self._breakers[provider.provider_id]
                if not await breaker.can_proceed():
                    log.debug("llm.provider.circuit_open", provider=provider.provider_id)
                    continue
                return provider

            raise NoSuitableProviderError(
                f"No healthy provider for tier={tier}, caps={caps}, agent={agent_id}"
            )

    def _rank_candidates(
        self,
        preferred_id: str,
        tier: ProviderTier,
        caps: set[Capability],
        max_budget_usd: float,
    ) -> list[LLMProvider]:
        """Return providers sorted: preferred first, then by cost ascending."""
        all_candidates = [
            p for p in self._providers.values()
            if p.tier == tier and p.supports(*caps)
            and p.cost_per_input_token * 4096 <= max_budget_usd  # rough budget filter
        ]
        preferred = [p for p in all_candidates if p.provider_id == preferred_id]
        rest = sorted(
            [p for p in all_candidates if p.provider_id != preferred_id],
            key=lambda p: p.cost_per_input_token,
        )
        return preferred + rest

    async def report_failure(self, provider_id: str, error: Exception) -> None:
        """Signal a provider failure to its circuit breaker."""
        if provider_id in self._breakers:
            await self._breakers[provider_id].record_failure()
            log.warning("llm.provider.failure", provider=provider_id, error=str(error))

    async def report_success(self, provider_id: str) -> None:
        """Signal a successful call to recover the circuit breaker."""
        if provider_id in self._breakers:
            await self._breakers[provider_id].record_success()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def complete_with_retry(
        self,
        provider: LLMProvider,
        request: CompletionRequest,
    ) -> CompletionResponse:
        """
        Execute a completion with tenacity retry (3 attempts, exponential backoff).
        Reports failure/success to the circuit breaker automatically.
        """
        try:
            response = await provider.complete(request)
            await self.report_success(provider.provider_id)
            return response
        except Exception as exc:
            await self.report_failure(provider.provider_id, exc)
            raise ProviderError(str(exc), provider_id=provider.provider_id) from exc
