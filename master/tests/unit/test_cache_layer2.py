"""
W8-2 / W8-3: a stable context prefix plus the (flagged) second Anthropic cache
breakpoint — the two pieces that make the injected Librarian block cacheable.
"""

from __future__ import annotations

from master.agents.librarian.context_builder import _build_summary, _stable_node_key
from master.llm.interfaces import CompletionRequest, Message
from master.llm.providers.anthropic import _CONTEXT_MARKER, AnthropicProvider

# ── W8-2: deterministic node ordering ─────────────────────────────────────────


def _nodes() -> list[dict]:
    return [
        {"id": "c", "name": "Cara", "relevance_score": 0.5},
        {"id": "a", "name": "Alice", "relevance_score": 0.9},
        {"id": "b", "name": "Bob", "relevance_score": 0.9},
    ]


def test_stable_key_orders_by_relevance_then_id() -> None:
    ordered = sorted(_nodes(), key=_stable_node_key)
    # 0.9 before 0.5; within 0.9, id 'a' before 'b'.
    assert [n["id"] for n in ordered] == ["a", "b", "c"]


def test_sort_is_order_independent() -> None:
    forward = sorted(_nodes(), key=_stable_node_key)
    reversed_in = sorted(list(reversed(_nodes())), key=_stable_node_key)
    assert [n["id"] for n in forward] == [n["id"] for n in reversed_in]


def test_summary_identical_for_shuffled_nodes() -> None:
    a = sorted(_nodes(), key=_stable_node_key)
    b = sorted(list(reversed(_nodes())), key=_stable_node_key)
    assert _build_summary("coding-agent", "chat", a, [], []) == _build_summary(
        "coding-agent", "chat", b, [], []
    )


def test_missing_relevance_and_id_is_safe() -> None:
    nodes = [{"name": "x"}, {"id": "z", "name": "y"}]
    ordered = sorted(nodes, key=_stable_node_key)  # must not raise
    assert len(ordered) == 2


# ── W8-3: second cache breakpoint, flag-gated ─────────────────────────────────


def _provider(second: bool) -> AnthropicProvider:
    return AnthropicProvider(
        model="claude-opus-4-8",
        litellm_proxy_url="http://x",
        litellm_api_key="k",
        second_cache_breakpoint=second,
    )


def _req() -> CompletionRequest:
    return CompletionRequest(
        messages=[
            Message(role="system", content="system prompt"),
            Message(role="user", content="Librarian context block", name=_CONTEXT_MARKER),
            Message(role="user", content="the actual question"),
        ],
        model="m",
    )


def _has_cache_control(entry: dict) -> bool:
    content = entry["content"]
    return isinstance(content, list) and any("cache_control" in block for block in content)


def test_system_always_cached_context_only_when_flag_on() -> None:
    msgs = _provider(second=True)._build_litellm_messages(_req())
    system, context, question = msgs
    assert _has_cache_control(system)
    assert _has_cache_control(context)
    assert not _has_cache_control(question)


def test_context_not_cached_when_flag_off() -> None:
    msgs = _provider(second=False)._build_litellm_messages(_req())
    system, context, question = msgs
    assert _has_cache_control(system)  # breakpoint 1 unchanged
    assert not _has_cache_control(context)  # no second breakpoint by default
    assert not _has_cache_control(question)


def test_unmarked_message_never_gets_second_breakpoint() -> None:
    # A large user message that is NOT tagged as context must not be cached,
    # even with the flag on — only the marked block qualifies.
    req = CompletionRequest(messages=[Message(role="user", content="x" * 5000)], model="m")
    msgs = _provider(second=True)._build_litellm_messages(req)
    assert not _has_cache_control(msgs[0])
