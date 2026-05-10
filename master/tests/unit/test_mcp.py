"""
master.tests.unit.test_mcp
============================
Unit tests for MCP client: schema validation, permission enforcement,
audit log stub, and tool-not-found handling.
All external services mocked — no network or DB required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from master.core.exceptions import (
    MCPPermissionError,
    MCPSchemaValidationError,
)
from master.mcp.client import AgentManifest, MCPClient
from master.mcp.interfaces import ToolSchema


def _make_client(
    agent_id: str = "personal-agent",
    allowed: dict[str, list[str]] | None = None,
    tools: list[ToolSchema] | None = None,
) -> tuple[MCPClient, AsyncMock]:
    """Build an MCPClient with mocked transport and audit logger."""
    allowed = allowed or {"gmail": ["read_emails", "send_email"]}
    tools = tools or [
        ToolSchema(
            name="read_emails",
            description="Read emails",
            input_schema={
                "type": "object",
                "properties": {"max_results": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
    ]

    manifest = AgentManifest(agent_id=agent_id, allowed_tools=allowed)
    transport = AsyncMock()
    transport.list_tools.return_value = tools
    transport.call_tool.return_value = {"emails": []}

    audit = AsyncMock()
    audit.log_event.return_value = 42

    client = MCPClient(
        agent_manifest=manifest,
        transports={"gmail": transport},
        server_configs={},
        audit_logger=audit,
    )
    return client, transport


class TestAgentManifest:
    def test_allowed_tool_returns_true(self) -> None:
        manifest = AgentManifest("personal-agent", {"gmail": ["read_emails"]})
        assert manifest.can_invoke("gmail", "read_emails") is True

    def test_disallowed_tool_returns_false(self) -> None:
        manifest = AgentManifest("personal-agent", {"gmail": ["read_emails"]})
        assert manifest.can_invoke("gmail", "delete_all") is False

    def test_wildcard_allows_all_tools(self) -> None:
        manifest = AgentManifest("admin-agent", {"gmail": ["*"]})
        assert manifest.can_invoke("gmail", "any_tool") is True

    def test_unknown_server_returns_false(self) -> None:
        manifest = AgentManifest("personal-agent", {"gmail": ["read_emails"]})
        assert manifest.can_invoke("github", "list_prs") is False


class TestMCPClientPermissions:
    @pytest.mark.asyncio
    async def test_invoke_disallowed_tool_raises_permission_error(self) -> None:
        client, _ = _make_client(allowed={"gmail": ["read_emails"]})
        with pytest.raises(MCPPermissionError):
            await client.invoke("gmail", "delete_all_emails", {})

    @pytest.mark.asyncio
    async def test_invoke_unknown_server_raises_permission_error(self) -> None:
        client, _ = _make_client()
        with pytest.raises(MCPPermissionError):
            await client.invoke("github", "list_prs", {"repo": "owner/repo"})


class TestMCPClientSchemaValidation:
    @pytest.mark.asyncio
    async def test_valid_input_succeeds(self) -> None:
        client, transport = _make_client()
        result = await client.invoke("gmail", "read_emails", {"max_results": 10})
        assert result.success is True
        transport.call_tool.assert_called_once_with("read_emails", {"max_results": 10})

    @pytest.mark.asyncio
    async def test_invalid_type_raises_schema_error(self) -> None:
        client, _ = _make_client()
        with pytest.raises(MCPSchemaValidationError):
            # max_results should be integer, not string
            await client.invoke("gmail", "read_emails", {"max_results": "ten"})

    @pytest.mark.asyncio
    async def test_extra_field_raises_schema_error(self) -> None:
        client, _ = _make_client()
        with pytest.raises(MCPSchemaValidationError):
            await client.invoke("gmail", "read_emails", {"max_results": 5, "forbidden_field": "x"})


class TestMCPClientAudit:
    @pytest.mark.asyncio
    async def test_successful_call_writes_audit_log(self) -> None:
        client, _ = _make_client()
        audit_mock = AsyncMock()
        audit_mock.log_event.return_value = 1
        client._audit = audit_mock

        await client.invoke("gmail", "read_emails", {})
        audit_mock.log_event.assert_called_once()
        call_kwargs = audit_mock.log_event.call_args.kwargs
        assert call_kwargs["event_type"] == "mcp.tool_call"
        assert "gmail" in call_kwargs["action"]


class TestMCPClientTransport:
    @pytest.mark.asyncio
    async def test_invoke_missing_transport_returns_error_result(self) -> None:
        """
        Verify that if a server is allowed but has no transport registered,
        invoke() returns a failed ToolInvocationResult instead of raising.
        """
        client, _ = _make_client(allowed={"gmail": ["read_emails"]})
        # Remove the registered transport to trigger the error path
        client._transports = {}

        # Pre-populate cache so it doesn't fail at _get_tool_schema (which also checks transports)
        client._tool_cache["gmail"] = {
            "read_emails": ToolSchema(
                name="read_emails",
                description="Read emails",
                input_schema={},
            )
        }

        result = await client.invoke("gmail", "read_emails", {})

        assert result.success is False
        assert result.error is not None
        assert "No transport registered for server 'gmail'" in result.error
        assert result.server_id == "gmail"
        assert result.tool_name == "read_emails"
