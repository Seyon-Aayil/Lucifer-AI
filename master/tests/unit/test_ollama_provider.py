"""
Unit tests for OllamaProvider: zero-cost DESKTOP-tier metadata and the mocked
``complete`` path. litellm is mocked — no local ollama daemon required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from master.llm.interfaces import (
    Capability,
    CompletionRequest,
    Message,
    ProviderTier,
)
from master.llm.providers.ollama import OllamaProvider


def _provider(model: str = "llama3.2") -> OllamaProvider:
    return OllamaProvider(model=model, ollama_base_url="http://127.0.0.1:11434")


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role="user", content="hi")],
        model="ignored",
        max_tokens=64,
    )


def _fake_response() -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=50, completion_tokens=20)
    message = SimpleNamespace(content="local answer", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


def test_is_zero_cost_desktop_tier() -> None:
    p = _provider("llama3.2")
    assert p.provider_id == "ollama-llama3.2"
    assert p.tier == ProviderTier.DESKTOP
    assert p.cost_per_input_token == 0.0
    assert p.cost_per_output_token == 0.0
    assert p.max_context_tokens == 8_000
    assert Capability.TEXT in p.capabilities
    # Local models don't advertise vision/function-calling here.
    assert Capability.FUNCTION_CALLING not in p.capabilities


@pytest.mark.asyncio
async def test_complete_routes_through_local_endpoint() -> None:
    p = _provider("llama3.2")
    with patch(
        "master.llm.providers.ollama.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response()),
    ) as mock_acompletion:
        resp = await p.complete(_request())

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "ollama/llama3.2"
    assert kwargs["api_base"] == "http://127.0.0.1:11434"
    assert resp.content == "local answer"
    assert resp.token_usage.input_tokens == 50
    assert resp.token_usage.output_tokens == 20
    # Zero cost regardless of token volume.
    assert resp.token_usage.cost(p.cost_per_input_token, p.cost_per_output_token) == 0.0


@pytest.mark.asyncio
async def test_count_tokens_delegates_to_litellm() -> None:
    p = _provider("llama3.2")
    with patch(
        "master.llm.providers.ollama.litellm.token_counter", return_value=11
    ) as mock_counter:
        assert await p.count_tokens("text") == 11
    assert mock_counter.call_args.kwargs["model"] == "ollama/llama3.2"
