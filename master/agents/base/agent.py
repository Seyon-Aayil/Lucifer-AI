"""
master.agents.base.agent
=========================
BaseAgent abstract class and all shared agent data types.
Every agent in the system inherits from BaseAgent and must satisfy
the AgentResponse contract — never raising from execute().
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.llm.interfaces import TokenUsage

log = get_logger(__name__)
tracer = get_tracer(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class RiskTier(str, Enum):
    """
    Risk classification for agent tasks.
    LOW/MEDIUM: autonomous execution.
    HIGH: HitL checkpoint required.
    CRITICAL: always blocked; manual only.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AgentSurface(str, Enum):
    """The device surface that originated the request."""
    WATCH = "watch"
    MOBILE = "mobile"
    DESKTOP = "desktop"
    MASTER = "master"
    CLI = "cli"
    WEB = "web"


# ── Memory Types ──────────────────────────────────────────────────────────────

@dataclass
class MemoryDelta:
    """
    A single write-back to the knowledge graph.
    Collected by the orchestrator and applied by LibrarianAgent.
    Never written directly by non-Librarian agents.
    """
    operation: Literal["upsert", "soft_delete", "edge_upsert", "edge_delete"]
    node_type: str | None = None
    node_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    edge_relation: str | None = None
    from_node_id: str | None = None
    to_node_id: str | None = None
    classification: str = "standard"


@dataclass
class ArtifactRef:
    """Reference to a stored artifact (file in MinIO)."""
    artifact_id: str
    content_hash: str
    storage_path: str
    mime_type: str
    title: str


# ── Token Budget ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TokenBudget:
    """Immutable budget constraints for a single agent turn."""
    input_limit: int
    output_limit: int
    max_cost_usd: float
    allow_compression: bool = True

    @classmethod
    def for_surface(cls, surface: AgentSurface) -> TokenBudget:
        """Return appropriate budget limits based on the calling surface."""
        budgets: dict[AgentSurface, tuple[int, int, float]] = {
            AgentSurface.WATCH:   (512,   128,   0.001),
            AgentSurface.MOBILE:  (2048,  512,   0.01),
            AgentSurface.DESKTOP: (8192,  2048,  0.05),
            AgentSurface.WEB:     (8192,  2048,  0.05),
            AgentSurface.CLI:     (16384, 4096,  0.10),
            AgentSurface.MASTER:  (131072, 8192, 1.00),
        }
        inp, out, cost = budgets[surface]
        return cls(input_limit=inp, output_limit=out, max_cost_usd=cost)


# ── Context Package ───────────────────────────────────────────────────────────

@dataclass
class ContextPackage:
    """
    Graph context assembled by LibrarianAgent for a requesting agent.
    Contains only nodes the requesting agent is permitted to see (ACL-filtered).
    """
    requesting_agent: str
    task_type: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""              # LLM-generated 200-token summary
    token_estimate: int = 0
    ttl_seconds: int = 300


# ── Request / Response ────────────────────────────────────────────────────────

@dataclass
class AgentRequest:
    """
    Immutable request payload passed to every agent's execute() method.
    Created by the orchestrator; never constructed within agents.
    """
    task_id: str
    agent_id: str
    intent: str
    context_package: ContextPackage
    token_budget: TokenBudget
    risk_tier: RiskTier
    surface: AgentSurface
    trace_id: str
    raw_input: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        agent_id: str,
        intent: str,
        raw_input: str,
        surface: AgentSurface,
        context_package: ContextPackage,
        risk_tier: RiskTier = RiskTier.LOW,
        trace_id: str | None = None,
    ) -> AgentRequest:
        """Convenience factory for tests and the orchestrator."""
        return cls(
            task_id=str(uuid.uuid4()),
            agent_id=agent_id,
            intent=intent,
            context_package=context_package,
            token_budget=TokenBudget.for_surface(surface),
            risk_tier=risk_tier,
            surface=surface,
            trace_id=trace_id or str(uuid.uuid4()),
            raw_input=raw_input,
        )


@dataclass
class AgentResponse:
    """
    Structured response from any agent's execute() method.
    Always returned — never raised. Use status='error' for failures.
    """
    task_id: str
    agent_id: str
    status: Literal["success", "partial", "escalate", "error"]
    result: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    memory_deltas: list[MemoryDelta] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=lambda: TokenUsage(0, 0))
    escalation_reason: str | None = None
    error_message: str | None = None
    cost_usd: float = 0.0


@dataclass
class AgentStreamChunk:
    """Single streaming chunk from stream_execute()."""
    task_id: str
    agent_id: str
    delta: str
    is_final: bool = False
    final_response: AgentResponse | None = None  # Populated when is_final=True


# ── Base Agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base for all Lucifer agents.
    Enforces:
      - Memory isolation (no direct DB access; all via LibrarianClient)
      - Token budget adherence
      - Telemetry emission on every action
      - AgentResponse contract from execute()

    Subclasses must implement execute().
    Optionally override stream_execute() for streaming surfaces.
    """

    def __init__(
        self,
        agent_id: str,
        librarian: Any,              # LibrarianClient — typed loosely to avoid circular import
        llm_registry: Any,           # ProviderRegistry
        telemetry_emitter: Any,      # TelemetryEmitter
    ) -> None:
        self.agent_id = agent_id
        self._librarian = librarian
        self._llm = llm_registry
        self._telemetry = telemetry_emitter

    @abstractmethod
    async def execute(self, request: AgentRequest) -> AgentResponse:
        """
        Core agent logic. Must:
        - Respect request.token_budget
        - Return AgentResponse (never raise)
        - Emit at least one telemetry event
        - Populate memory_deltas for any knowledge graph writes
        """

    async def stream_execute(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentStreamChunk]:
        """
        Streaming variant. Default: executes synchronously and wraps in a
        single chunk with is_final=True.
        Override for true streaming.
        """
        response = await self.execute(request)
        content = response.result.get("content", "")
        yield AgentStreamChunk(
            task_id=request.task_id,
            agent_id=self.agent_id,
            delta=content,
            is_final=True,
            final_response=response,
        )

    def _emit(self, event_type: str, request: AgentRequest, **metadata: Any) -> None:
        """
        Emit a structured telemetry event. Always called; never swallowed.
        Use this for agent.start, agent.complete, agent.error, agent.escalate.
        """
        try:
            self._telemetry.emit_event(
                event_type=event_type,
                agent_id=self.agent_id,
                task_id=request.task_id,
                trace_id=request.trace_id,
                metadata={k: str(v) for k, v in metadata.items()},
            )
        except Exception as exc:
            # Telemetry must never crash agent execution
            log.warning("telemetry.emit.failed", agent=self.agent_id, error=str(exc))

    def _error_response(
        self, request: AgentRequest, message: str, *, partial_result: dict[str, Any] | None = None
    ) -> AgentResponse:
        """Helper to build a consistent error response."""
        log.error("agent.error", agent=self.agent_id, task=request.task_id, message=message)
        return AgentResponse(
            task_id=request.task_id,
            agent_id=self.agent_id,
            status="error",
            result=partial_result or {},
            error_message=message,
        )

    def _escalate_response(self, request: AgentRequest, reason: str) -> AgentResponse:
        """Helper to build an escalation response for HitL."""
        log.info("agent.escalate", agent=self.agent_id, reason=reason)
        return AgentResponse(
            task_id=request.task_id,
            agent_id=self.agent_id,
            status="escalate",
            escalation_reason=reason,
        )
