"""
master.core.exceptions
======================
Domain-specific exception hierarchy for Lucifer AI.
All application errors derive from LuciferError.
HTTP translation happens at the router layer only — never here.
"""
from __future__ import annotations


class LuciferError(Exception):
    """Base exception for all Lucifer AI domain errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.__class__.__name__
        self.message = message


# ── Auth ─────────────────────────────────────────────────────────────────────

class AuthError(LuciferError):
    """Base class for authentication / authorisation failures."""


class InvalidTokenError(AuthError):
    """JWT is malformed, expired, or has an invalid signature."""


class TokenExpiredError(AuthError):
    """JWT has passed its expiry time."""


class DeviceRevokedError(AuthError):
    """Device has been explicitly revoked; reject all tokens from it."""


class DeviceNotRegisteredError(AuthError):
    """Device ID is not in the device registry."""


class InsufficientPermissionsError(AuthError):
    """Caller does not have the required role or capability."""


# ── Privacy & Safety ─────────────────────────────────────────────────────────

class PIIDetectedError(LuciferError):
    """
    PII detected in content destined for a cloud LLM.
    Raised when user has not explicitly consented to external dispatch.
    """


class DataClassificationError(LuciferError):
    """
    Attempted to send Restricted or Secret data to a cloud LLM.
    These classes must stay on-device or use local models only.
    """


class ContentPolicyViolationError(LuciferError):
    """Content flagged by safety classifier; request blocked."""


# ── Budget & Cost ─────────────────────────────────────────────────────────────

class BudgetExceededError(LuciferError):
    """Agent or global daily spend cap reached."""

    def __init__(self, agent_id: str, limit_usd: float, spent_usd: float) -> None:
        super().__init__(
            f"Agent '{agent_id}' budget exhausted: "
            f"spent ${spent_usd:.4f} of ${limit_usd:.4f} daily limit"
        )
        self.agent_id = agent_id
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd


class TokenBudgetExceededError(LuciferError):
    """Context window or token budget exceeded for a single request."""


# ── LLM & Providers ───────────────────────────────────────────────────────────

class ProviderError(LuciferError):
    """Base class for LLM provider failures."""

    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id


class ProviderUnavailableError(ProviderError):
    """Provider is unreachable or circuit-breaker is open."""


class ProviderRateLimitError(ProviderError):
    """Provider returned 429 — rate limited."""


class ProviderResponseValidationError(ProviderError):
    """Provider response failed schema or hallucination checks."""


class NoSuitableProviderError(LuciferError):
    """No provider could be selected for the given constraints."""


# ── Agents ────────────────────────────────────────────────────────────────────

class AgentError(LuciferError):
    """Base class for agent execution failures."""


class AgentTimeoutError(AgentError):
    """Agent execution exceeded its time limit."""


class AgentEscalationRequired(AgentError):
    """
    Risk tier requires human-in-the-loop approval.
    Not a real error — signals the orchestrator to insert a HitL checkpoint.
    """

    def __init__(self, agent_id: str, reason: str) -> None:
        super().__init__(f"Escalation required from '{agent_id}': {reason}")
        self.agent_id = agent_id
        self.reason = reason


# ── Memory & Graph ────────────────────────────────────────────────────────────

class LibrarianError(LuciferError):
    """Base class for Librarian Agent failures."""


class NodeNotFoundError(LibrarianError):
    """Requested graph node does not exist."""


class PermissionDeniedError(LibrarianError):
    """
    Agent attempted to access a graph node outside its ACL.
    See ARCHITECTURE.md §5.3 for the access control matrix.
    """

    def __init__(self, agent_id: str, node_type: str, operation: str) -> None:
        super().__init__(
            f"Agent '{agent_id}' denied '{operation}' on '{node_type}' nodes"
        )
        self.agent_id = agent_id
        self.node_type = node_type
        self.operation = operation


# ── MCP ───────────────────────────────────────────────────────────────────────

class MCPError(LuciferError):
    """Base class for MCP server communication failures."""


class MCPToolNotFoundError(MCPError):
    """Tool name not registered in the server manifest."""


class MCPSchemaValidationError(MCPError):
    """Tool input failed JSON Schema validation."""


class MCPPermissionError(MCPError):
    """Agent manifest does not permit this tool invocation."""


# ── Sync ─────────────────────────────────────────────────────────────────────

class SyncError(LuciferError):
    """Base class for edge↔master sync failures."""


class ConflictResolutionError(SyncError):
    """Vector clock conflict could not be resolved deterministically."""
