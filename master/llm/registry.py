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
    ProviderSelection,
    ProviderTier,
)

log = get_logger(__name__)
tracer = get_tracer(__name__)


def _is_refusal(response: CompletionResponse) -> bool:
    """
    True if the response is a safety refusal. LiteLLM maps Anthropic
    stop_reason="refusal" (and Google/OpenAI safety blocks) to
    finish_reason="content_filter".
    """
    return response.finish_reason == "content_filter"


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
        strong_model_id: str = "anthropic-claude-opus-4-8",
        weak_model_id: str = "anthropic-claude-haiku-4-5",
        refusal_fallback: bool = True,
    ) -> None:
        self._providers: dict[str, LLMProvider] = {p.provider_id: p for p in providers}
        self._breakers: dict[str, CircuitBreaker] = {
            p.provider_id: CircuitBreaker(p.provider_id) for p in providers
        }
        self._routellm_threshold = routellm_threshold
        self._strong_model_id = strong_model_id
        self._weak_model_id = weak_model_id
        self._refusal_fallback = refusal_fallback
        # Lazy-loaded RouteLLM controller
        self._routellm: Any | None = None

    @property
    def strong_model_id(self) -> str:
        """The provider id RouteLLM routes complex queries to."""
        return self._strong_model_id

    def set_strong_model(self, model_id: str) -> None:
        """
        Promote a model to the 'strong' tier at runtime — the activation hook
        for the model-upgrade pipeline. Takes effect for subsequent `select`
        calls on this instance.
        """
        log.info("llm.registry.strong_model_changed", old=self._strong_model_id, new=model_id)
        self._strong_model_id = model_id

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
            for model in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
                providers.append(
                    AnthropicProvider(
                        model=model,
                        litellm_proxy_url=settings.litellm_proxy_url,
                        litellm_api_key=settings.litellm_master_key,
                    )
                )

        # OpenAI, Google, Ollama adapters follow the same pattern.
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
            for model in ("gemini-2.5-pro", "gemini-2.0-flash"):
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
            refusal_fallback=settings.refusal_fallback_enabled,
        )

    async def _routellm_score(self, query: str) -> float:
        """
        Score query complexity using RouteLLM controller.
        Returns 0.0–1.0. Above threshold → strong model.
        Falls back to 0.5 (ambiguous) if RouteLLM unavailable.
        """
        try:
            if self._routellm is None:
                from routellm.controller import Controller

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

    def complexity_to_effort(self, score: float) -> str | None:
        """
        Map a RouteLLM complexity score (0.0–1.0) to a reasoning-effort level.

        Returns None below the routing threshold (the weak tier is chosen and
        does not support the effort dial). Above the threshold, sub-buckets the
        strong tier so cheap-but-complex queries don't pay full effort.
        """
        if score < self._routellm_threshold:
            return None
        if score < 0.7:
            return "medium"
        if score < 0.85:
            return "high"
        return "xhigh"

    async def select(
        self,
        query: str,
        tier: ProviderTier = ProviderTier.MASTER,
        required_capabilities: set[Capability] | None = None,
        agent_id: str = "unknown",
        max_budget_usd: float = 1.0,
    ) -> ProviderSelection:
        """
        Select the most appropriate provider for a request.
        1. RouteLLM scores complexity → selects preferred model + effort level.
        2. Filters candidates by tier + capabilities + circuit state + budget.
        3. Falls back through chain if preferred is unavailable.
        Returns the provider plus the reasoning effort derived from the score.
        Raises: NoSuitableProviderError if no provider can serve the request.
        """
        with tracer.start_as_current_span("llm.registry.select"):
            caps = required_capabilities or {Capability.TEXT}

            # RouteLLM: pick preferred provider ID + effort level
            score = await self._routellm_score(query)
            preferred_id = (
                self._strong_model_id if score >= self._routellm_threshold else self._weak_model_id
            )
            effort = self.complexity_to_effort(score)
            log.debug(
                "routellm.route",
                score=round(score, 3),
                preferred=preferred_id,
                effort=effort,
                agent=agent_id,
            )

            # Build candidate list: preferred first, then others by tier
            candidates = self._rank_candidates(preferred_id, tier, caps, max_budget_usd)

            for provider in candidates:
                breaker = self._breakers[provider.provider_id]
                if not await breaker.can_proceed():
                    log.debug("llm.provider.circuit_open", provider=provider.provider_id)
                    continue
                return ProviderSelection(provider=provider, effort=effort)

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
            p
            for p in self._providers.values()
            if p.tier == tier
            and p.supports(*caps)
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

    async def _record_spend(
        self,
        spend_tracker: Any | None,
        agent_id: str | None,
        provider: LLMProvider,
        response: CompletionResponse,
    ) -> None:
        """Record actual spend for a completed call, if a tracker is wired."""
        if spend_tracker and agent_id:
            cost = (
                response.token_usage.input_tokens * provider.cost_per_input_token
                + response.token_usage.output_tokens * provider.cost_per_output_token
            )
            await spend_tracker.record_spend(agent_id, cost)

    def _should_refusal_fallback(self, provider: LLMProvider, response: CompletionResponse) -> bool:
        """
        Whether a safety refusal should be re-served on the strong model.

        Gated to MASTER (cloud) providers so local-only calls (e.g. the health
        agent's Ollama path) are never re-routed to a cloud model. No fallback
        when the strong model itself refused (terminal).
        """
        return (
            self._refusal_fallback
            and _is_refusal(response)
            and provider.tier == ProviderTier.MASTER
            and provider.provider_id != self._strong_model_id
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def complete_with_retry(
        self,
        provider: LLMProvider,
        request: CompletionRequest,
        agent_id: str | None = None,
        spend_tracker: Any | None = None,
    ) -> CompletionResponse:
        """
        Execute a completion with tenacity retry (3 attempts, exponential backoff).
        Pre-flight budget check via SpendTracker if provided.
        Reports failure/success to the circuit breaker automatically.
        """
        # ── Budget pre-flight ──────────────────────────────────────────────────
        if spend_tracker and agent_id:
            estimated = provider.cost_per_input_token * request.max_tokens
            await spend_tracker.check_budget(agent_id, estimated)

        try:
            response = await provider.complete(request)
            await self.report_success(provider.provider_id)
            await self._record_spend(spend_tracker, agent_id, provider, response)

            # ── Refusal fallback ────────────────────────────────────────────────
            # A safety refusal is a successful 200 (not an exception), so it is
            # handled here, after the call. Re-serve once on the strong model.
            if self._should_refusal_fallback(provider, response):
                fb = self._providers.get(self._strong_model_id)
                if fb is not None and await self._breakers[fb.provider_id].can_proceed():
                    log.info(
                        "llm.refusal_fallback",
                        from_provider=provider.provider_id,
                        to_provider=fb.provider_id,
                        agent=agent_id,
                    )
                    fb_response = await fb.complete(request)
                    await self.report_success(fb.provider_id)
                    await self._record_spend(spend_tracker, agent_id, fb, fb_response)
                    return fb_response

            return response
        except Exception as exc:
            await self.report_failure(provider.provider_id, exc)
            raise ProviderError(str(exc), provider_id=provider.provider_id) from exc


# ── Shared process-wide registry ─────────────────────────────────────────────
# The orchestrator and the model-upgrade scheduler share one instance so that a
# runtime `set_strong_model` (model promotion) takes effect for live routing.

_shared_registry: ProviderRegistry | None = None


def shared_registry() -> ProviderRegistry:
    """Return the process-wide ProviderRegistry, building it on first use."""
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = ProviderRegistry.from_settings()
    return _shared_registry


def reset_shared_registry() -> None:
    """Drop the cached shared registry (tests / explicit reload)."""
    global _shared_registry
    _shared_registry = None
