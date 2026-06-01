"""
master.tests.conftest
=====================
Shared pytest fixtures for all test suites.
Unit tests: mock all external services.
Integration tests: use real Docker services (mark with @pytest.mark.integration).
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# Force test environment before any app code is imported
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-32-bytes-minimum!!")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://lucifer:test@localhost:5432/lucifer_test"
)
os.environ.setdefault("NEO4J_PASSWORD", "testpassword")
os.environ.setdefault("MINIO_SECRET_KEY", "testpassword")
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-local-test-key-16chars")
os.environ.setdefault("MEM0_API_KEY", "test-mem0-key")
os.environ.setdefault("ZEP_API_KEY", "test-zep-key")


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def mock_librarian():
    """Mock librarian client used by orchestrator-level tests."""
    client = AsyncMock()
    from master.agents.base.agent import ContextPackage

    client.get_context_package.return_value = ContextPackage(
        requesting_agent="test-agent",
        task_type="test",
        nodes=[],
        edges=[],
        summary="Test context",
        token_estimate=100,
    )
    client.apply_memory_deltas.return_value = None
    return client


@pytest.fixture
def mock_llm_registry():
    """Mock ProviderRegistry that returns a fake provider."""
    registry = AsyncMock()
    provider = AsyncMock()
    from master.llm.interfaces import CompletionResponse, TokenUsage

    provider.provider_id = "test-provider"
    provider.complete.return_value = CompletionResponse(
        content="Test LLM response",
        model="test-model",
        provider_id="test-provider",
        token_usage=TokenUsage(input_tokens=100, output_tokens=50),
        finish_reason="stop",
    )
    registry.select.return_value = provider
    registry.complete_with_retry.return_value = provider.complete.return_value
    return registry


@pytest.fixture
def mock_telemetry():
    """Mock TelemetryEmitter — verify events are emitted but don't export."""
    emitter = MagicMock()
    emitter.emit_event = MagicMock()
    return emitter


@pytest.fixture
def mock_redis():
    """Mock async Redis client."""
    redis = AsyncMock()
    redis.exists.return_value = 0  # No revocations by default
    redis.set.return_value = True
    redis.get.return_value = None
    redis.incrbyfloat.return_value = 0.0
    redis.ping.return_value = True
    return redis


@pytest.fixture
def sample_agent_request():
    """A minimal valid AgentRequest for testing agents."""
    from master.agents.base.agent import (
        AgentRequest,
        AgentSurface,
        ContextPackage,
        RiskTier,
        TokenBudget,
    )

    return AgentRequest(
        task_id="test-task-id",
        agent_id="personal-agent",
        intent="chat",
        context_package=ContextPackage(
            requesting_agent="personal-agent",
            task_type="chat",
        ),
        token_budget=TokenBudget(
            input_limit=8192,
            output_limit=2048,
            max_cost_usd=0.05,
        ),
        risk_tier=RiskTier.LOW,
        surface=AgentSurface.WEB,
        trace_id="test-trace-id",
        raw_input="Hello, how are you?",
    )
