"""Unit tests for master.orchestrator.agent_pool."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from master.agents.base.agent import AgentRequest, AgentResponse, BaseAgent
from master.orchestrator.agent_pool import AgentPool


class _StubAgent(BaseAgent):
    AGENT_ID: ClassVar[str] = "stub-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


class _AnotherAgent(BaseAgent):
    AGENT_ID: ClassVar[str] = "another-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


class _UnnamedAgent(BaseAgent):
    AGENT_ID: ClassVar[str] = ""

    async def execute(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


class _FallbackAgent(BaseAgent):
    AGENT_ID: ClassVar[str] = "personal-agent"

    async def execute(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


# ── Registration ──────────────────────────────────────────────────────────────


def test_register_adds_class_under_agent_id():
    pool = AgentPool()
    pool.register(_StubAgent)
    assert "stub-agent" in pool.registered_ids()


def test_register_rejects_empty_agent_id():
    pool = AgentPool()
    with pytest.raises(ValueError, match="AGENT_ID"):
        pool.register(_UnnamedAgent)


def test_register_overwrites_existing():
    pool = AgentPool()
    pool.register(_StubAgent)

    class _Override(BaseAgent):
        AGENT_ID: ClassVar[str] = "stub-agent"

        async def execute(self, request):  # pragma: no cover
            raise NotImplementedError

    pool.register(_Override)
    # Last registration wins
    assert pool._classes["stub-agent"] is _Override


def test_registered_ids_returns_list():
    pool = AgentPool()
    pool.register(_StubAgent)
    pool.register(_AnotherAgent)
    ids = pool.registered_ids()
    assert set(ids) == {"stub-agent", "another-agent"}


# ── Resolution ────────────────────────────────────────────────────────────────


def _deps() -> dict[str, Any]:
    return {
        "librarian": object(),
        "llm_registry": object(),
        "telemetry_emitter": object(),
        "mcp_client": object(),
    }


def test_resolve_known_agent_returns_instance():
    pool = AgentPool()
    pool.register(_StubAgent)
    agent = pool.resolve("stub-agent", **_deps())
    assert isinstance(agent, _StubAgent)
    assert agent.agent_id == "stub-agent"


def test_resolve_injects_dependencies():
    pool = AgentPool()
    pool.register(_StubAgent)
    deps = _deps()
    agent = pool.resolve("stub-agent", **deps)
    assert agent._librarian is deps["librarian"]
    assert agent._llm is deps["llm_registry"]
    assert agent._telemetry is deps["telemetry_emitter"]
    assert agent._mcp is deps["mcp_client"]


def test_resolve_unknown_falls_back_to_personal_agent():
    pool = AgentPool()
    pool.register(_FallbackAgent)
    agent = pool.resolve("nonexistent-agent", **_deps())
    assert isinstance(agent, _FallbackAgent)


def test_resolve_unknown_with_no_fallback_raises():
    pool = AgentPool()
    pool.register(_StubAgent)  # no personal-agent registered
    with pytest.raises(RuntimeError, match="fallback"):
        pool.resolve("nonexistent", **_deps())


def test_resolve_returns_fresh_instance_each_call():
    pool = AgentPool()
    pool.register(_StubAgent)
    a = pool.resolve("stub-agent", **_deps())
    b = pool.resolve("stub-agent", **_deps())
    assert a is not b
