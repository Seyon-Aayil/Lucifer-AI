"""Unit tests for master.agents.librarian.access_control."""

from __future__ import annotations

import pytest

from master.agents.librarian.access_control import (
    NodeType,
    check_permission,
    filter_nodes_for_agent,
)
from master.core.exceptions import PermissionDeniedError


# ── Librarian bypass ──────────────────────────────────────────────────────────


def test_librarian_has_full_access():
    # Librarian agent never raises regardless of node/operation
    check_permission("librarian", NodeType.HEALTH_RECORD, "write")
    check_permission("librarian-v2", NodeType.FINANCIAL, "write")
    check_permission("librarian", NodeType.PERSON, "read")


# ── Per-agent matrix ──────────────────────────────────────────────────────────


def test_financial_agent_can_write_financial():
    check_permission("financial-agent", NodeType.FINANCIAL, "write")
    check_permission("financial-agent", NodeType.FINANCIAL, "read")


def test_financial_agent_cannot_write_person():
    with pytest.raises(PermissionDeniedError):
        check_permission("financial-agent", NodeType.PERSON, "write")
    # Read is allowed
    check_permission("financial-agent", NodeType.PERSON, "read")


def test_financial_agent_cannot_access_health():
    with pytest.raises(PermissionDeniedError):
        check_permission("financial-agent", NodeType.HEALTH_RECORD, "read")


def test_health_agent_can_write_health():
    check_permission("health-agent", NodeType.HEALTH_RECORD, "write")


def test_health_agent_cannot_access_financial():
    with pytest.raises(PermissionDeniedError):
        check_permission("health-agent", NodeType.FINANCIAL, "read")
    with pytest.raises(PermissionDeniedError):
        check_permission("health-agent", NodeType.FINANCIAL, "write")


def test_personal_agent_read_only_health_and_financial():
    check_permission("personal-agent", NodeType.HEALTH_RECORD, "read")
    check_permission("personal-agent", NodeType.FINANCIAL, "read")
    with pytest.raises(PermissionDeniedError):
        check_permission("personal-agent", NodeType.HEALTH_RECORD, "write")
    with pytest.raises(PermissionDeniedError):
        check_permission("personal-agent", NodeType.FINANCIAL, "write")


def test_news_agent_only_artifact_write_no_read():
    # News agent matrix: Artifact={write}, News={read,write}
    check_permission("news-agent", NodeType.ARTIFACT, "write")
    check_permission("news-agent", NodeType.NEWS, "read")
    with pytest.raises(PermissionDeniedError):
        check_permission("news-agent", NodeType.ARTIFACT, "read")


def test_research_agent_can_write_concepts():
    check_permission("research-agent", NodeType.CONCEPT, "write")
    check_permission("research-agent", NodeType.NEWS, "write")
    with pytest.raises(PermissionDeniedError):
        check_permission("research-agent", NodeType.TASK, "write")


# ── Unknown agents ────────────────────────────────────────────────────────────


def test_unknown_agent_denied():
    with pytest.raises(PermissionDeniedError):
        check_permission("ghost-agent", NodeType.PERSON, "read")


# ── Prefix-match behaviour ────────────────────────────────────────────────────


def test_versioned_agent_id_inherits_acl():
    # "financial-agent-v2" matches the "financial-agent" prefix
    check_permission("financial-agent-v2", NodeType.FINANCIAL, "write")
    with pytest.raises(PermissionDeniedError):
        check_permission("financial-agent-v2", NodeType.HEALTH_RECORD, "read")


# ── Unknown node types ────────────────────────────────────────────────────────


def test_unknown_node_type_denied():
    with pytest.raises(PermissionDeniedError):
        check_permission("personal-agent", "UnknownType", "read")


# ── Error payload ─────────────────────────────────────────────────────────────


def test_permission_error_carries_context():
    with pytest.raises(PermissionDeniedError) as exc_info:
        check_permission("financial-agent", NodeType.HEALTH_RECORD, "write")
    err = exc_info.value
    assert err.agent_id == "financial-agent"


# ── filter_nodes_for_agent ────────────────────────────────────────────────────


def test_filter_drops_unauthorised_nodes():
    nodes = [
        {"labels": [NodeType.PERSON], "id": "p1"},
        {"labels": [NodeType.FINANCIAL], "id": "f1"},
        {"labels": [NodeType.HEALTH_RECORD], "id": "h1"},
    ]
    # health-agent: read on Person + HealthRecord; cannot read Financial
    result = filter_nodes_for_agent("health-agent", nodes, operation="read")
    ids = {n["id"] for n in result}
    assert ids == {"p1", "h1"}


def test_filter_uses_type_field_when_no_labels():
    nodes = [
        {"type": NodeType.NEWS, "id": "n1"},
        {"type": NodeType.HEALTH_RECORD, "id": "h1"},
    ]
    result = filter_nodes_for_agent("financial-agent", nodes, operation="read")
    assert {n["id"] for n in result} == {"n1"}


def test_filter_empty_input():
    assert filter_nodes_for_agent("personal-agent", [], operation="read") == []


def test_filter_silent_on_unknown_agent():
    # Unknown agent → all nodes filtered out, but no exception raised
    nodes = [{"labels": [NodeType.PERSON], "id": "p1"}]
    assert filter_nodes_for_agent("ghost", nodes) == []


def test_filter_write_operation():
    nodes = [
        {"labels": [NodeType.FINANCIAL], "id": "f1"},
        {"labels": [NodeType.PERSON], "id": "p1"},
    ]
    # financial-agent can write Financial but not Person
    result = filter_nodes_for_agent("financial-agent", nodes, operation="write")
    assert [n["id"] for n in result] == ["f1"]
