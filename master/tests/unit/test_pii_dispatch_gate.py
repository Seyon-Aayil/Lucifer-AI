"""
W1: PII cloud-dispatch gate in ProviderRegistry.complete_with_retry.

Property under test: personal-graph context assembled into a CompletionRequest
cannot reach a MASTER-tier (cloud) provider carrying raw PII — it is reversibly
pseudonymised at the dispatch choke point. The same request to a local
(DESKTOP/Ollama) provider is dispatched unmodified.

NER is not exercised here (no spaCy model in CI); the regex engine covers
emails/phones, and a hand-rolled scanner stands in for NER to prove person
names are tokenised on the same path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from master.api.middleware.pii_scanner import (
    PIICategory,
    PIIMatch,
    PIIScanner,
    ScanResult,
)
from master.llm.interfaces import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ProviderTier,
    TokenUsage,
)
from master.llm.registry import ProviderRegistry

_STRONG_ID = "anthropic-claude-opus-4-8"
_WEAK_ID = "anthropic-claude-haiku-4-5"
_OLLAMA_ID = "ollama-llama3"

_EMAIL = "jane.doe@example.com"
_PHONE = "555-867-5309"


def _make_provider(
    provider_id: str, tier: ProviderTier, finish_reason: str = "stop", content: str = "ok"
) -> MagicMock:
    p = MagicMock()
    p.provider_id = provider_id
    p.tier = tier
    p.cost_per_input_token = 0.0
    p.cost_per_output_token = 0.0
    resp = CompletionResponse(
        content=content,
        model=provider_id,
        provider_id=provider_id,
        token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        finish_reason=finish_reason,
    )
    p.complete = AsyncMock(return_value=resp)
    return p


def _registry(*providers: MagicMock, gate_enabled: bool = True, scanner: object | None = None):
    return ProviderRegistry(
        providers=list(providers),
        strong_model_id=_STRONG_ID,
        pii_gate_enabled=gate_enabled,
        # Deterministic, CI-safe: regex scanner unless a stand-in is injected.
        pii_scanner=scanner or PIIScanner(use_ner=False),
    )


def _req(*contents: str, model: str = "x") -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role="user", content=c) for c in contents], model=model
    )


def _sent_request(provider: MagicMock) -> CompletionRequest:
    """The CompletionRequest the provider actually received."""
    return provider.complete.await_args.args[0]


def _all_content(request: CompletionRequest) -> str:
    return "\n".join(m.content for m in request.messages if isinstance(m.content, str))


# ── cloud dispatch is scrubbed ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_request_cannot_carry_raw_email() -> None:
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud)

    await reg.complete_with_retry(cloud, _req(f"Email Jane at {_EMAIL}"))

    sent = _all_content(_sent_request(cloud))
    assert _EMAIL not in sent
    assert "EMAIL_1" in sent


@pytest.mark.asyncio
async def test_cloud_request_tokenises_multiple_categories() -> None:
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud)

    await reg.complete_with_retry(cloud, _req(f"{_EMAIL} / {_PHONE}"))

    sent = _all_content(_sent_request(cloud))
    assert _EMAIL not in sent and _PHONE not in sent
    assert "EMAIL_1" in sent and "PHONE_1" in sent


@pytest.mark.asyncio
async def test_same_value_gets_stable_token_across_messages() -> None:
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud)

    await reg.complete_with_retry(
        cloud, _req(f"System note about {_EMAIL}", f"User asks about {_EMAIL}")
    )

    for msg in _sent_request(cloud).messages:
        assert _EMAIL not in msg.content
        assert "EMAIL_1" in msg.content


@pytest.mark.asyncio
async def test_person_name_tokenised_via_ner_engine() -> None:
    """A PERSON match (as NER would produce) is tokenised on the same path."""

    class _NameScanner:
        def scan(self, text: str) -> ScanResult:
            idx = text.find("Jane Doe")
            if idx == -1:
                return ScanResult(has_pii=False, masked_text=text)
            match = PIIMatch(
                category=PIICategory.PERSON_NAME,
                start=idx,
                end=idx + len("Jane Doe"),
                value="Jane Doe",
                masked="[PERSON_NAME]",
            )
            return ScanResult(has_pii=True, matches=[match], masked_text=text)

    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud, scanner=_NameScanner())

    await reg.complete_with_retry(cloud, _req("Remind Jane Doe about lunch"))

    sent = _all_content(_sent_request(cloud))
    assert "Jane Doe" not in sent
    assert "PERSON_NAME_1" in sent


# ── local dispatch is untouched ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_provider_receives_raw_request() -> None:
    ollama = _make_provider(_OLLAMA_ID, ProviderTier.DESKTOP)
    reg = _registry(ollama)

    original = _req(f"Email Jane at {_EMAIL}")
    await reg.complete_with_retry(ollama, original)

    sent = _sent_request(ollama)
    assert sent is original  # unmodified, same object
    assert _EMAIL in _all_content(sent)


# ── response de-pseudonymisation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_echoed_token_restored_in_response() -> None:
    # Model answers referencing the token it was given.
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER, content="I emailed EMAIL_1 for you.")
    reg = _registry(cloud)

    result = await reg.complete_with_retry(cloud, _req(f"Email Jane at {_EMAIL}"))

    assert result.content == f"I emailed {_EMAIL} for you."


# ── gate disabled / clean path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_disabled_passes_raw_request() -> None:
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud, gate_enabled=False)

    original = _req(f"Email Jane at {_EMAIL}")
    await reg.complete_with_retry(cloud, original)

    sent = _sent_request(cloud)
    assert sent is original
    assert _EMAIL in _all_content(sent)


@pytest.mark.asyncio
async def test_clean_request_passes_through_unchanged() -> None:
    cloud = _make_provider(_STRONG_ID, ProviderTier.MASTER)
    reg = _registry(cloud)

    original = _req("What is the capital of France?")
    result = await reg.complete_with_retry(cloud, original)

    # No PII → same object dispatched, response returned verbatim.
    assert _sent_request(cloud) is original
    assert result.content == "ok"


# ── the gate holds even through the refusal fallback hop ───────────────────────


@pytest.mark.asyncio
async def test_refusal_fallback_dispatches_pseudonymised_payload() -> None:
    weak = _make_provider(_WEAK_ID, ProviderTier.MASTER, "content_filter", "refused")
    strong = _make_provider(_STRONG_ID, ProviderTier.MASTER, "stop", "done")
    reg = _registry(weak, strong)

    await reg.complete_with_retry(weak, _req(f"Email Jane at {_EMAIL}"))

    # Both the primary and the fallback cloud call must see the scrubbed payload.
    strong.complete.assert_awaited_once()
    assert _EMAIL not in _all_content(_sent_request(strong))
    assert "EMAIL_1" in _all_content(_sent_request(strong))
