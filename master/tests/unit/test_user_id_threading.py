"""A4: user_id threads through ContextBuilder and AgentRequest (per-user memory scope)."""

from unittest.mock import AsyncMock, patch

import pytest

from master.agents.base.agent import (
    AgentRequest,
    AgentSurface,
    ContextPackage,
    RiskTier,
)
from master.agents.librarian.context_builder import ContextBuilder


def _build_context_builder():
    """ContextBuilder with mocked Mem0/Zep clients (no env / network)."""
    with (
        patch("master.agents.librarian.context_builder.Mem0Client") as mem0_cls,
        patch("master.agents.librarian.context_builder.ZepClient") as zep_cls,
    ):
        mem0_cls.return_value.search = AsyncMock(return_value=[])
        zep_cls.return_value.search = AsyncMock(return_value=[])
        cb = ContextBuilder(graph_client=None)  # gc=None → Neo4j fetch returns []
    return cb


@pytest.mark.asyncio
async def test_build_scopes_stores_to_user_id():
    cb = _build_context_builder()
    await cb.build("personal-agent", "chat", user_id="alice")
    assert cb._mem0.search.await_args.kwargs["user_id"] == "alice"
    assert cb._zep.search.await_args.kwargs["user_id"] == "alice"


@pytest.mark.asyncio
async def test_build_defaults_to_single_operator():
    cb = _build_context_builder()
    await cb.build("personal-agent", "chat")
    assert cb._mem0.search.await_args.kwargs["user_id"] == "lucifer-user"
    assert cb._zep.search.await_args.kwargs["user_id"] == "lucifer-user"


def test_agent_request_carries_user_id():
    pkg = ContextPackage(requesting_agent="personal-agent", task_type="chat")
    req = AgentRequest.create(
        agent_id="personal-agent",
        intent="chat",
        raw_input="hi",
        surface=AgentSurface.WEB,
        context_package=pkg,
        risk_tier=RiskTier.LOW,
        user_id="alice",
    )
    assert req.user_id == "alice"


def test_agent_request_user_id_defaults_none():
    pkg = ContextPackage(requesting_agent="personal-agent", task_type="chat")
    req = AgentRequest.create(
        agent_id="personal-agent",
        intent="chat",
        raw_input="hi",
        surface=AgentSurface.WEB,
        context_package=pkg,
    )
    assert req.user_id is None
