"""
master.orchestrator.graph
==========================
LangGraph state graph for Lucifer orchestration.

Graph nodes (in order):
  classify → context_inject → budget_plan → route → execute
  → [hitl?] → synthesize → memory_write

HitL checkpoint: inserted automatically when risk_tier >= HIGH.
Edges are conditional — failures route to an error_sink node.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from master.agents.base.agent import (
    AgentRequest,
    AgentSurface,
    RiskTier,
    TokenBudget,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.orchestrator.state import OrchestratorState

log = get_logger(__name__)
tracer = get_tracer(__name__)

# Risk tiers that require human approval before execution
_HITL_TIERS = {RiskTier.HIGH, RiskTier.CRITICAL}


# ── Node Implementations ──────────────────────────────────────────────────────

async def classify_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Classify the raw input into: intent, agent_id, risk_tier.
    Uses a local Ollama model with rule-based fallback.
    """
    with tracer.start_as_current_span("orchestrator.classify"):
        raw = state.get("raw_input", "")
        intent, agent_id, risk_tier = await _ollama_intent_classifier(raw)
        requires_hitl = risk_tier in _HITL_TIERS

        log.info(
            "orchestrator.classify",
            intent=intent,
            agent=agent_id,
            risk=risk_tier.value,
            hitl=requires_hitl,
        )
        return {
            "intent": intent,
            "agent_id": agent_id,
            "risk_tier": risk_tier,
            "requires_hitl": requires_hitl,
        }


async def context_inject_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Call LibrarianAgent to fetch the context package for the selected agent+intent.
    Package is ACL-filtered and token-budgeted by the Librarian.
    """
    with tracer.start_as_current_span("orchestrator.context_inject"):
        agent_id = state.get("agent_id", "personal-agent")
        intent = state.get("intent", "chat")

        from master.agents.base.agent import ContextPackage
        from master.agents.librarian.context_builder import ContextBuilder
        from master.agents.librarian.graph_client import GraphClient

        context = ContextPackage(requesting_agent=agent_id, task_type=intent)
        try:
            gc = GraphClient.from_settings()
            context = await ContextBuilder(gc).build(agent_id, intent)
            await gc.close()
        except Exception as exc:
            log.warning("orchestrator.context_inject.fallback", error=str(exc))

        log.debug("orchestrator.context_injected", agent=agent_id)
        return {"context_package": context}


async def budget_plan_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Calculate token budget for this request based on surface + daily spend remaining.
    """
    with tracer.start_as_current_span("orchestrator.budget_plan"):
        surface = state.get("surface", AgentSurface.WEB)
        budget = TokenBudget.for_surface(surface)
        log.debug("orchestrator.budget_planned", inputs=budget.input_limit, outputs=budget.output_limit)
        return {"token_budget": budget}


async def route_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Build the typed AgentRequest from accumulated state.
    """
    with tracer.start_as_current_span("orchestrator.route"):
        request = AgentRequest(
            task_id=state.get("task_id", str(uuid.uuid4())),
            agent_id=state.get("agent_id", "personal-agent"),
            intent=state.get("intent", "chat"),
            context_package=state["context_package"],
            token_budget=state["token_budget"],
            risk_tier=state.get("risk_tier", RiskTier.LOW),
            surface=state.get("surface", AgentSurface.WEB),
            trace_id=state.get("trace_id", str(uuid.uuid4())),
            raw_input=state.get("raw_input", ""),
        )
        return {"agent_request": request}


async def execute_node(state: OrchestratorState, config: dict[str, Any]) -> dict[str, Any]:
    """
    Dispatch the agent request to the selected agent via AgentPool.
    Falls back to PersonalAgent for unregistered agent IDs.
    """
    with tracer.start_as_current_span("orchestrator.execute"):
        request: AgentRequest = state["agent_request"]

        from master.agents.base.agent import AgentResponse
        from master.llm.registry import ProviderRegistry
        from master.mcp.registry import MCPServerRegistry
        from master.orchestrator.agent_pool import AgentPool

        llm_registry = ProviderRegistry.from_settings()

        mcp_client = None
        mcp_registry = (config or {}).get("configurable", {}).get("mcp_registry")
        if mcp_registry is not None:
            try:
                manifest = MCPServerRegistry.load_manifest(request.agent_id)
                mcp_client = mcp_registry.get_client(manifest)
            except Exception as exc:
                log.warning("execute_node.mcp_client_failed", error=str(exc))

        class OTelEmitter:
            def emit_event(self, **kwargs: Any) -> None:
                log.info("agent.telemetry.event", **kwargs)

        agent = _agent_pool.resolve(
            request.agent_id,
            librarian=None,
            llm_registry=llm_registry,
            telemetry_emitter=OTelEmitter(),
            mcp_client=mcp_client,
        )

        try:
            response = await agent.execute(request)
        except Exception as exc:
            log.error("execute_node.agent.failed", error=str(exc))
            response = AgentResponse(
                task_id=request.task_id,
                agent_id=request.agent_id,
                status="error",
                error_message=str(exc),
            )

        log.info("orchestrator.execute.done", agent=request.agent_id, status=response.status)
        return {
            "agent_response": response,
            "memory_deltas": response.memory_deltas,
        }


async def hitl_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Human-in-the-loop checkpoint.
    Interrupts the graph and waits for external approval signal.
    LangGraph's interrupt() mechanism handles the pause.
    """
    from langgraph.types import interrupt

    with tracer.start_as_current_span("orchestrator.hitl"):
        response: Any = state.get("agent_response")
        reason = response.escalation_reason if response else "High-risk action"
        log.info("orchestrator.hitl.waiting", reason=reason)

        approval: dict[str, Any] = interrupt(
            {"reason": reason, "agent_id": state.get("agent_id"), "task_id": state.get("task_id")}
        )
        approved = approval.get("approved", False)
        return {"hitl_approved": approved, "hitl_feedback": approval.get("feedback")}


async def synthesize_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Format the agent response for the calling surface.
    Merges multi-agent outputs if applicable.
    """
    with tracer.start_as_current_span("orchestrator.synthesize"):
        response = state.get("agent_response")
        content = ""
        if response and response.status in ("success", "partial"):
            content = response.result.get("content", "")
        elif response and response.status == "error":
            content = f"I encountered an error: {response.error_message}"
        elif response and response.status == "escalate":
            content = f"This action requires approval: {response.escalation_reason}"

        return {"final_output": content, "output_metadata": {"status": response.status if response else "error"}}


async def memory_write_node(state: OrchestratorState) -> dict[str, Any]:
    """
    Apply memory_deltas to all three stores (Neo4j, Mem0, Zep) via MemoryWriter.
    """
    with tracer.start_as_current_span("orchestrator.memory_write"):
        deltas = state.get("memory_deltas", [])
        if not deltas:
            return {"memory_written": True}

        from master.agents.librarian.graph_client import GraphClient
        from master.agents.librarian.memory_writer import MemoryWriter

        log.info("orchestrator.memory_write", delta_count=len(deltas))
        try:
            gc = GraphClient.from_settings()
            writer = MemoryWriter(graph_client=gc)
            await writer.apply_deltas(
                deltas,
                agent_id=state.get("agent_id", ""),
            )
            await gc.close()
        except Exception as exc:
            log.error("orchestrator.memory_write_failed", error=str(exc))

        return {"memory_written": True}


async def error_sink_node(state: OrchestratorState) -> dict[str, Any]:
    """Catch-all error node. Logs and returns error output."""
    log.error("orchestrator.error", error=state.get("error"), task=state.get("task_id"))
    return {
        "final_output": "An unexpected error occurred. Please try again.",
        "output_metadata": {"status": "error"},
        "memory_written": False,
    }


# ── Routing Functions ─────────────────────────────────────────────────────────

def _should_hitl(state: OrchestratorState) -> str:
    """After execute: route to hitl if escalation needed, error_sink if failed, else synthesize."""
    response = state.get("agent_response")
    if not response:
        return "error_sink"
    if response.status == "escalate":
        return "hitl"
    if response.status == "error":
        return "error_sink"
    return "synthesize"


def _hitl_approved(state: OrchestratorState) -> str:
    """After hitl: proceed to synthesize if approved, else error."""
    return "synthesize" if state.get("hitl_approved", False) else "error_sink"


# ── Agent Pool (module-level singleton) ──────────────────────────────────────

from master.orchestrator.agent_pool import AgentPool  # noqa: E402

_agent_pool: AgentPool = AgentPool.build_default()


# ── Graph Construction ────────────────────────────────────────────────────────


def build_graph(checkpointer: Any = None) -> Any:
    """
    Build and compile the Lucifer orchestration graph.

    Args:
        checkpointer: LangGraph checkpointer instance. Defaults to in-memory
            MemorySaver. Pass an AsyncPostgresSaver from the FastAPI lifespan
            for persistent multi-turn conversation state.

    Returns:
        A compiled StateGraph ready for invocation.
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

    graph = StateGraph(OrchestratorState)

    # Register nodes
    graph.add_node("classify", classify_node)
    graph.add_node("context_inject", context_inject_node)
    graph.add_node("budget_plan", budget_plan_node)
    graph.add_node("route", route_node)
    graph.add_node("execute", execute_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_node("memory_write", memory_write_node)
    graph.add_node("error_sink", error_sink_node)

    # Linear edges
    graph.add_edge(START, "classify")
    graph.add_edge("classify", "context_inject")
    graph.add_edge("context_inject", "budget_plan")
    graph.add_edge("budget_plan", "route")
    graph.add_edge("route", "execute")

    # Conditional: execute → hitl or synthesize
    graph.add_conditional_edges(
        "execute", _should_hitl,
        {"hitl": "hitl", "synthesize": "synthesize", "error_sink": "error_sink"},
    )

    # Conditional: hitl → synthesize or error_sink
    graph.add_conditional_edges(
        "hitl", _hitl_approved,
        {"synthesize": "synthesize", "error_sink": "error_sink"},
    )

    # Final linear edges
    graph.add_edge("synthesize", "memory_write")
    graph.add_edge("memory_write", END)
    graph.add_edge("error_sink", END)

    return graph.compile(checkpointer=checkpointer)


_INTENT_CANDIDATES: list[tuple[str, str, RiskTier]] = [
    ("chat",               "personal-agent",   RiskTier.LOW),
    ("schedule_meeting",   "personal-agent",   RiskTier.MEDIUM),
    ("summarise_calendar", "personal-agent",   RiskTier.LOW),
    ("daily_briefing",     "personal-agent",   RiskTier.LOW),
    ("code_assist",        "coding-agent",     RiskTier.LOW),
    ("health_query",       "health-agent",     RiskTier.MEDIUM),
    ("financial_query",    "financial-agent",  RiskTier.HIGH),
    ("research",           "research-agent",   RiskTier.LOW),
]

_INTENT_CLASSIFIER_PROMPT = (
    "Classify the user input into one of these intent/agent/risk combinations:\n"
    + "\n".join(
        f"- intent={i}, agent={a}, risk={r.value}"
        for i, a, r in _INTENT_CANDIDATES
    )
    + '\n\nUser input: "{raw_input}"\n\n'
    'Reply with JSON only: {{"intent": "...", "agent_id": "...", "risk_tier": "..."}}'
)


async def _ollama_intent_classifier(raw_input: str) -> tuple[str, str, RiskTier]:
    """
    Classify intent via a local Ollama model (mistral).
    Falls back to rule-based classifier on any error or timeout.
    """
    from master.core.config import get_settings

    settings = get_settings()
    prompt = _INTENT_CLASSIFIER_PROMPT.format(raw_input=raw_input)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": "mistral", "prompt": prompt, "stream": False, "format": "json"},
            )
            resp.raise_for_status()
            result: dict[str, Any] = json.loads(resp.json().get("response", "{}"))
            intent = result.get("intent", "chat")
            agent_id = result.get("agent_id", "personal-agent")
            risk_str = result.get("risk_tier", "low")
            valid_risk = {t.value for t in RiskTier}
            risk_tier = RiskTier(risk_str) if risk_str in valid_risk else RiskTier.LOW
            return intent, agent_id, risk_tier
    except Exception as exc:
        log.debug("orchestrator.classify.ollama_fallback", error=str(exc))
        return _simple_intent_classifier(raw_input)


def _simple_intent_classifier(raw_input: str) -> tuple[str, str, RiskTier]:
    """
    Rule-based fallback classifier. Used when Ollama is unavailable.
    Returns: (intent, agent_id, risk_tier).
    """
    text = raw_input.lower()
    if any(kw in text for kw in ["code", "python", "debug", "function", "pr", "github"]):
        return "code_assist", "coding-agent", RiskTier.LOW
    if any(kw in text for kw in ["health", "heart", "sleep", "medication", "doctor"]):
        return "health_query", "health-agent", RiskTier.MEDIUM
    if any(kw in text for kw in ["finance", "spend", "budget", "bank", "invest"]):
        return "financial_query", "financial-agent", RiskTier.HIGH
    if any(kw in text for kw in ["research", "find", "search", "summarise", "paper"]):
        return "research", "research-agent", RiskTier.LOW
    return "chat", "personal-agent", RiskTier.LOW
