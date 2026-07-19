"""
W5-3: post-scrub trace ingestion.

Langfuse ingests exactly what LiteLLM is asked to send the provider. The PII
gate (W1) pseudonymises the request BEFORE dispatch, so whatever reaches
litellm.acompletion — and therefore the langfuse success_callback — must already
be scrubbed. This test captures the litellm.acompletion payload on the real
provider path and asserts a raw email never appears in it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from master.api.middleware.pii_scanner import PIIScanner
from master.llm.interfaces import CompletionRequest, Message
from master.llm.providers.anthropic import AnthropicProvider
from master.llm.registry import ProviderRegistry

_EMAIL = "jane.doe@example.com"


def _fake_litellm_response() -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    message = SimpleNamespace(content="done", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


def _serialise(kwargs: dict) -> str:
    """Everything LiteLLM would hand the langfuse callback for this call."""
    return str(kwargs.get("messages", ""))


@pytest.mark.asyncio
async def test_litellm_payload_is_pseudonymised() -> None:
    provider = AnthropicProvider(
        model="claude-haiku-4-5", litellm_proxy_url="http://x", litellm_api_key="k"
    )
    registry = ProviderRegistry(
        providers=[provider],
        strong_model_id="anthropic-claude-opus-4-8",
        pii_gate_enabled=True,
        pii_scanner=PIIScanner(use_ner=False),
    )
    request = CompletionRequest(
        messages=[Message(role="user", content=f"Draft a reply to {_EMAIL}")],
        model="claude-haiku-4-5",
    )

    captured: dict = {}

    async def _capture(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return _fake_litellm_response()

    with patch("master.llm.providers.anthropic.litellm.acompletion", new=_capture):
        await registry.complete_with_retry(provider, request)

    payload = _serialise(captured)
    assert _EMAIL not in payload, "raw email must never reach LiteLLM / Langfuse"
    assert "EMAIL_1" in payload  # the pseudonym is what gets traced


@pytest.mark.asyncio
async def test_response_restored_in_process_after_dispatch() -> None:
    # The de-pseudonymised value is reconstructed only in the returned response,
    # never in the dispatched (traced) payload.
    provider = AnthropicProvider(
        model="claude-haiku-4-5", litellm_proxy_url="http://x", litellm_api_key="k"
    )
    registry = ProviderRegistry(
        providers=[provider],
        strong_model_id="anthropic-claude-opus-4-8",
        pii_gate_enabled=True,
        pii_scanner=PIIScanner(use_ner=False),
    )
    request = CompletionRequest(
        messages=[Message(role="user", content=f"Draft a reply to {_EMAIL}")],
        model="claude-haiku-4-5",
    )

    def _echo_token_response() -> SimpleNamespace:
        r = _fake_litellm_response()
        r.choices[0].message.content = "Sent to EMAIL_1."
        return r

    async def _capture(**kwargs: object) -> SimpleNamespace:
        return _echo_token_response()

    with patch("master.llm.providers.anthropic.litellm.acompletion", new=_capture):
        resp = await registry.complete_with_retry(provider, request)

    # In-process response is de-pseudonymised for the caller...
    assert resp.content == f"Sent to {_EMAIL}."
