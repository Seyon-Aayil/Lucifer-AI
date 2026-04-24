"""
master.agents.librarian.access_control
========================================
ACL enforcement for Librarian Agent context package generation.
Maps (agent_id, node_type) → permitted operations (read/write/none).
Raises PermissionDeniedError on violations.
See ARCHITECTURE.md §5.3 for the authoritative matrix.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from master.core.exceptions import PermissionDeniedError
from master.core.logging import get_logger

log = get_logger(__name__)

Operation = Literal["read", "write"]


class NodeType(str, Enum):
    PERSON = "Person"
    PLACE = "Place"
    EVENT = "Event"
    CONCEPT = "Concept"
    ARTIFACT = "Artifact"
    HEALTH_RECORD = "HealthRecord"
    FINANCIAL = "Financial"
    TASK = "Task"
    NEWS = "News"


class RelationType(str, Enum):
    WORKS_WITH = "WORKS_WITH"
    AUTHORED = "AUTHORED"
    ABOUT = "ABOUT"
    MEMBER_OF = "MEMBER_OF"
    LIVES_AT = "LIVES_AT"
    HAPPENED_AT = "HAPPENED_AT"
    PART_OF = "PART_OF"
    RELATED_TO = "RELATED_TO"
    FOLLOWS = "FOLLOWS"
    MENTIONS = "MENTIONS"


# ── ACL Matrix ────────────────────────────────────────────────────────────────
# Format: agent_id_prefix → {node_type → set of allowed operations}
# "read:*" means read all fields; "read:name" means only specific fields (advisory)

_ACL: dict[str, dict[str, set[str]]] = {
    "financial-agent": {
        NodeType.PERSON:       {"read"},
        NodeType.FINANCIAL:    {"read", "write"},
        NodeType.ARTIFACT:     {"read"},
        NodeType.TASK:         {"read", "write"},
        NodeType.NEWS:         {"read"},
    },
    "health-agent": {
        NodeType.PERSON:       {"read"},
        NodeType.HEALTH_RECORD:{"read", "write"},
        NodeType.ARTIFACT:     {"read"},
        NodeType.TASK:         {"read", "write"},
        NodeType.NEWS:         {"read"},
    },
    "coding-agent": {
        NodeType.PERSON:       {"read"},
        NodeType.ARTIFACT:     {"read", "write"},
        NodeType.TASK:         {"read", "write"},
        NodeType.NEWS:         {"read"},
    },
    "personal-agent": {
        NodeType.PERSON:       {"read", "write"},
        NodeType.PLACE:        {"read", "write"},
        NodeType.EVENT:        {"read", "write"},
        NodeType.HEALTH_RECORD:{"read"},
        NodeType.FINANCIAL:    {"read"},
        NodeType.ARTIFACT:     {"read", "write"},
        NodeType.TASK:         {"read", "write"},
        NodeType.NEWS:         {"read", "write"},
    },
    "research-agent": {
        NodeType.PERSON:       {"read"},
        NodeType.CONCEPT:      {"read", "write"},
        NodeType.ARTIFACT:     {"read", "write"},
        NodeType.TASK:         {"read"},
        NodeType.NEWS:         {"read", "write"},
    },
    "news-agent": {
        NodeType.ARTIFACT:     {"write"},
        NodeType.NEWS:         {"read", "write"},
    },
    # Librarian has full access — not listed here; checked separately
}


def check_permission(agent_id: str, node_type: str, operation: Operation) -> None:
    """
    Enforce ACL for a given (agent_id, node_type, operation) triple.
    Librarian agent always passes.
    Raises PermissionDeniedError if the operation is not permitted.
    """
    if agent_id.startswith("librarian"):
        return  # Librarian has full access

    # Exact match first, then prefix match (e.g. "financial-agent" matches "financial-agent-v2")
    agent_acl: dict[str, set[str]] | None = None
    for key in _ACL:
        if agent_id == key or agent_id.startswith(key):
            agent_acl = _ACL[key]
            break

    if agent_acl is None:
        raise PermissionDeniedError(agent_id, node_type, operation)

    allowed_ops = agent_acl.get(node_type, set())
    if operation not in allowed_ops:
        raise PermissionDeniedError(agent_id, node_type, operation)

    log.debug(
        "acl.permitted",
        agent=agent_id,
        node_type=node_type,
        operation=operation,
    )


def filter_nodes_for_agent(
    agent_id: str, nodes: list[dict], operation: Operation = "read"
) -> list[dict]:
    """
    Filter a list of raw node dicts to only those the agent can access.
    Silently drops nodes the agent cannot read — does not raise.
    """
    allowed: list[dict] = []
    for node in nodes:
        node_type = node.get("labels", [None])[0] or node.get("type", "")
        try:
            check_permission(agent_id, node_type, operation)
            allowed.append(node)
        except PermissionDeniedError:
            pass
    return allowed
