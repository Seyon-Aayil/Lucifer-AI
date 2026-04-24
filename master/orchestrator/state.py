"""
master.orchestrator.state
==========================
LangGraph state TypedDict for the Lucifer orchestration graph.
All nodes in the graph read/write from this shared state object.
The state is the single source of truth for a task's lifecycle.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from master.agents.base.agent import (
    AgentRequest,
    AgentResponse,
    AgentSurface,
    ContextPackage,
    MemoryDelta,
    RiskTier,
    TokenBudget,
)


class OrchestratorState(TypedDict, total=False):
    """
    Shared state object passed through every node of the LangGraph graph.
    Fields are progressively populated as the graph executes.
    total=False: all fields are optional (set by individual nodes).
    """
    # ── Input (set at graph entry) ──────────────────────────────────────────
    task_id: str
    raw_input: str
    surface: AgentSurface
    device_id: str | None
    session_id: str | None
    trace_id: str

    # ── Intent Classification (set by classify node) ────────────────────────
    intent: str                         # "chat", "schedule", "research", ...
    agent_id: str                       # Which agent should handle this
    risk_tier: RiskTier
    requires_hitl: bool

    # ── Context (set by context_inject node) ────────────────────────────────
    context_package: ContextPackage

    # ── Budget (set by budget_plan node) ────────────────────────────────────
    token_budget: TokenBudget

    # ── Agent Request (set by route node) ───────────────────────────────────
    agent_request: AgentRequest

    # ── Execution (set by execute node) ─────────────────────────────────────
    agent_response: AgentResponse | None

    # ── HitL (set by hitl node) ─────────────────────────────────────────────
    hitl_approved: bool
    hitl_feedback: str | None

    # ── Synthesis (set by synthesize node) ──────────────────────────────────
    final_output: str
    output_metadata: dict[str, Any]

    # ── Memory (set by memory_write node) ───────────────────────────────────
    memory_deltas: list[MemoryDelta]
    memory_written: bool

    # ── Error tracking ───────────────────────────────────────────────────────
    error: str | None
    retry_count: int

    # ── Conversation history (accumulated across turns) ──────────────────────
    messages: Annotated[list[dict[str, Any]], add_messages]
