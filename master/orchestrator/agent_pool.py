"""
master.orchestrator.agent_pool
================================
AgentPool: runtime registry mapping agent_id → BaseAgent subclass.

Replaces the hardcoded `PersonalAgent` instantiation in `execute_node`.
Agents are registered at pool construction time; new agents auto-register
when their module is importable (supports incremental Phase 3 rollout).

Usage:
    pool = AgentPool.build_default()
    agent = pool.resolve("personal-agent", llm_registry=..., ...)
"""

from __future__ import annotations

import importlib
from typing import Any

from master.agents.base.agent import BaseAgent
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)

# (module_path, class_name) for every specialist agent
_AGENT_MODULES: list[tuple[str, str]] = [
    ("master.agents.personal.agent", "PersonalAgent"),
    ("master.agents.coding.agent", "CodingAgent"),
    ("master.agents.financial.agent", "FinancialAgent"),
    ("master.agents.health.agent", "HealthAgent"),
    ("master.agents.research.agent", "ResearchAgent"),
]

_FALLBACK_AGENT_ID = "personal-agent"


class AgentPool:
    """
    Runtime registry of available agents.

    - Register agents by class (keyed on AGENT_ID).
    - Resolve agent_id → fresh BaseAgent instance with injected dependencies.
    - Falls back to PersonalAgent when an unknown agent_id is requested
      (covers the Phase 3 transition while specialist agents are built).
    - Supports hot-reload: call reload() to re-import all registered modules.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[BaseAgent]] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, agent_class: type[BaseAgent]) -> None:
        """Register an agent class using its AGENT_ID class variable."""
        if not agent_class.AGENT_ID:
            raise ValueError(f"Agent class {agent_class.__name__} must define a non-empty AGENT_ID")
        self._classes[agent_class.AGENT_ID] = agent_class
        log.info("agent_pool.registered", agent_id=agent_class.AGENT_ID)

    def reload(self) -> None:
        """
        Re-import all agent modules and re-register their classes.
        Safe to call at runtime; updates the registry in-place.
        """
        self._classes.clear()
        for module_path, class_name in _AGENT_MODULES:
            _try_register(self, module_path, class_name)
        log.info("agent_pool.reloaded", count=len(self._classes))

    # ── Resolution ────────────────────────────────────────────────────────────

    def resolve(
        self,
        agent_id: str,
        *,
        librarian: Any,
        llm_registry: Any,
        telemetry_emitter: Any,
        mcp_client: Any = None,
    ) -> BaseAgent:
        """
        Instantiate and return the agent for the given agent_id.

        If the requested agent is not registered, logs a warning and falls
        back to PersonalAgent so that the request still completes.
        """
        with tracer.start_as_current_span("agent_pool.resolve"):
            cls = self._classes.get(agent_id)
            if cls is None:
                log.warning(
                    "agent_pool.unknown_agent",
                    requested=agent_id,
                    fallback=_FALLBACK_AGENT_ID,
                )
                cls = self._classes.get(_FALLBACK_AGENT_ID)
                if cls is None:
                    raise RuntimeError(
                        f"AgentPool has no fallback: '{_FALLBACK_AGENT_ID}' is not registered"
                    )

            return cls(
                agent_id=agent_id,
                librarian=librarian,
                llm_registry=llm_registry,
                telemetry_emitter=telemetry_emitter,
                mcp_client=mcp_client,
            )

    def registered_ids(self) -> list[str]:
        return list(self._classes.keys())

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def build_default(cls) -> AgentPool:
        """
        Build an AgentPool with all known agents registered.
        Agents whose modules are not yet importable are silently skipped,
        allowing incremental Phase 3 rollout.
        """
        pool = cls()
        for module_path, class_name in _AGENT_MODULES:
            _try_register(pool, module_path, class_name)
        log.info("agent_pool.built", registered=pool.registered_ids())
        return pool


# ── Helpers ───────────────────────────────────────────────────────────────────


def _try_register(pool: AgentPool, module_path: str, class_name: str) -> None:
    """Import module_path and register class_name; silently skip on failure."""
    try:
        mod = importlib.import_module(module_path)
        agent_cls: type[BaseAgent] = getattr(mod, class_name)
        pool.register(agent_cls)
    except (ImportError, AttributeError, ValueError) as exc:
        log.debug(
            "agent_pool.skip_unavailable",
            module=module_path,
            cls=class_name,
            reason=str(exc),
        )
