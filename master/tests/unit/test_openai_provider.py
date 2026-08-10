"""
Unit tests for OpenAIProvider: pricing, tier/capabilities, the mocked
``complete`` path (usage → TokenUsage, tool-call mapping) and token counting.
Mirrors the AnthropicProvider test style — litellm is mocked, no network.
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
from master.llm.providers.openai import OpenAIProvider


def _provider(model: str = "gpt-4o") -> OpenAIProvider:
    return OpenAIProvider(model=model, litellm_proxy_url="http://x", litellm_api_key="k")


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message(role="user", content="hi")],
        model="ignored",
        max_tokens=64,
        temperature=0.5,
    )


def _fake_response(*, tool_calls: object = None) -> SimpleNamespace:
    usage = SimpleNamespace(prompt_tokens=30, completion_tokens=12)
    message = SimpleNamespace(content="hello", tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(usage=usage, choices=[choice])


def test_pricing_and_metadata() -> None:
    p = _provider("gpt-4o")
    assert p.provider_id == "openai-gpt-4o"
    assert p.tier == ProviderTier.MASTER
    assert p.cost_per_input_token == 5.0 / 1_000_000
    assert p.cost_per_output_token == 15.0 / 1_000_000
    assert p.max_context_tokens == 128_000
    assert Capability.FUNCTION_CALLING in p.capabilities


def test_unknown_model_is_zero_cost() -> None:
    p = _provider("gpt-unknown")
    assert p.cost_per_input_token == 0.0
    assert p.cost_per_output_token == 0.0


@pytest.mark.asyncio
async def test_complete_maps_usage_and_model() -> None:
    p = _provider("gpt-4o")
    with patch(
        "master.llm.providers.openai.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response()),
    ) as mock_acompletion:
        resp = await p.complete(_request())

    assert mock_acompletion.call_args.kwargs["model"] == "openai/gpt-4o"
    assert resp.content == "hello"
    assert resp.provider_id == "openai-gpt-4o"
    assert resp.token_usage.input_tokens == 30
    assert resp.token_usage.output_tokens == 12
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_complete_maps_tool_calls() -> None:
    tc = SimpleNamespace(model_dump=lambda: {"id": "call_1", "type": "function"})
    p = _provider("gpt-4o")
    with patch(
        "master.llm.providers.openai.litellm.acompletion",
        new=AsyncMock(return_value=_fake_response(tool_calls=[tc])),
    ):
        resp = await p.complete(_request())

    assert resp.tool_calls == [{"id": "call_1", "type": "function"}]


@pytest.mark.asyncio
async def test_count_tokens_delegates_to_litellm() -> None:
    p = _provider("gpt-4o")
    with patch("master.llm.providers.openai.litellm.token_counter", return_value=7) as mock_counter:
        assert await p.count_tokens("some text") == 7
    assert mock_counter.call_args.kwargs["model"] == "openai/gpt-4o"
