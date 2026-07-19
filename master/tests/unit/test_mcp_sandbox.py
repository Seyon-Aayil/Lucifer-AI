"""
W2-4: CodingAgent code execution runs through the `sandbox` MCP server —
the same path as every other tool (manifest ACL → schema validation → HMAC
audit → DockerTransport isolation), not a bespoke AutoGen executor.

Docker is not available in CI, so the transport is faked; the tests pin the
wiring (registry builds a DockerTransport, ACL admits only the coding agent,
schema is enforced, audit fires) rather than a live container.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from master.core.exceptions import MCPPermissionError, MCPSchemaValidationError
from master.mcp.client import AgentManifest, MCPClient
from master.mcp.interfaces import ToolInvocationResult, ToolSchema, TransportType
from master.mcp.registry import MCPServerRegistry

_REPO_ROOT = Path(__file__).parents[3]
_MANIFEST_YAML = _REPO_ROOT / "infra" / "mcp_servers.yaml"
_CODING_MANIFEST = _REPO_ROOT / "master" / "agents" / "coding" / "agent_manifest.json"

_EXECUTE_SCHEMA = ToolSchema(
    name="execute_code",
    description="Run code in a sandbox",
    input_schema={
        "type": "object",
        "required": ["language", "code"],
        "properties": {
            "language": {"type": "string", "enum": ["python", "bash", "node"]},
            "code": {"type": "string"},
            "timeout_seconds": {"type": "integer", "maximum": 60},
            "stdin": {"type": "string"},
        },
        "additionalProperties": False,
    },
    requires_confirmation=True,
)


# ── Manifest / config wiring ──────────────────────────────────────────────────


def test_sandbox_server_registered_as_docker_stdio() -> None:
    registry = MCPServerRegistry.from_config(AsyncMock(), config_path=_MANIFEST_YAML)
    cfg = registry._configs.get("sandbox")  # type: ignore[attr-defined]
    assert cfg is not None, "sandbox server missing from mcp_servers.yaml"
    assert cfg.transport == TransportType.STDIO
    assert cfg.docker_image == "lucifer/mcp-sandbox:latest"
    assert cfg.allowed_agents == ["coding-agent"]


def test_registry_builds_docker_transport_for_sandbox() -> None:
    from master.mcp.transport.docker import DockerTransport

    registry = MCPServerRegistry.from_config(AsyncMock(), config_path=_MANIFEST_YAML)
    cfg = registry._configs["sandbox"]  # type: ignore[attr-defined]
    transport = registry._build_transport(cfg)  # type: ignore[attr-defined]
    # DockerTransport, not SSE — code execution is sandboxed. Not connected here.
    assert isinstance(transport, DockerTransport)
    assert transport._image == "lucifer/mcp-sandbox:latest"  # type: ignore[attr-defined]


def test_coding_manifest_grants_sandbox_execute_code() -> None:
    data = json.loads(_CODING_MANIFEST.read_text())
    assert "execute_code" in data["allowed_tools"].get("sandbox", [])


# ── ACL: only the coding agent may execute code ───────────────────────────────


def _client(agent_id: str, allowed: dict[str, list[str]]) -> tuple[MCPClient, AsyncMock]:
    transport = AsyncMock()
    transport.list_tools.return_value = [_EXECUTE_SCHEMA]
    transport.call_tool.return_value = {"stdout": "hi\n", "stderr": "", "exit_code": 0}
    audit = AsyncMock()
    audit.log_event.return_value = 7
    client = MCPClient(
        agent_manifest=AgentManifest(agent_id=agent_id, allowed_tools=allowed),
        transports={"sandbox": transport},
        server_configs={},
        audit_logger=audit,
    )
    return client, transport


@pytest.mark.asyncio
async def test_coding_agent_can_execute_code() -> None:
    client, transport = _client("coding-agent", {"sandbox": ["execute_code"]})
    result = await client.invoke(
        "sandbox", "execute_code", {"language": "python", "code": "print(1)"}
    )
    assert result.success
    transport.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_coding_agent_denied_sandbox() -> None:
    # A different agent with no sandbox grant must be refused by the ACL.
    client, _ = _client("personal-agent", {"gmail": ["read_emails"]})
    with pytest.raises(MCPPermissionError):
        await client.invoke("sandbox", "execute_code", {"language": "python", "code": "print(1)"})


# ── Schema + audit hold on the sandbox path ───────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_language_rejected_by_schema() -> None:
    client, _ = _client("coding-agent", {"sandbox": ["execute_code"]})
    with pytest.raises(MCPSchemaValidationError):
        await client.invoke("sandbox", "execute_code", {"language": "ruby", "code": "puts 1"})


@pytest.mark.asyncio
async def test_missing_code_rejected_by_schema() -> None:
    client, _ = _client("coding-agent", {"sandbox": ["execute_code"]})
    with pytest.raises(MCPSchemaValidationError):
        await client.invoke("sandbox", "execute_code", {"language": "python"})


@pytest.mark.asyncio
async def test_execution_is_audited() -> None:
    client, _ = _client("coding-agent", {"sandbox": ["execute_code"]})
    await client.invoke("sandbox", "execute_code", {"language": "python", "code": "print(1)"})
    audit: AsyncMock = client._audit  # type: ignore[assignment]
    audit.log_event.assert_awaited_once()
    kwargs = audit.log_event.call_args.kwargs
    assert kwargs["event_type"] == "mcp.tool_call"
    assert kwargs["action"] == "sandbox.execute_code"
    assert kwargs["agent_id"] == "coding-agent"


# ── CodingAgent routes run_code to the sandbox server ─────────────────────────


def _coding_agent(mcp: object) -> object:
    from master.agents.coding.agent import CodingAgent

    return CodingAgent(
        agent_id="coding-agent",
        librarian=MagicMock(),
        llm_registry=MagicMock(),
        telemetry_emitter=MagicMock(),
        mcp_client=mcp,
    )


@pytest.mark.asyncio
async def test_run_code_invokes_sandbox_and_formats_output() -> None:
    mcp = AsyncMock()
    mcp.invoke.return_value = ToolInvocationResult(
        tool_name="execute_code",
        server_id="sandbox",
        success=True,
        output={"stdout": "3\n", "stderr": "", "exit_code": 0},
    )
    agent = _coding_agent(mcp)
    request = MagicMock()
    request.raw_input = "print(1 + 2)"

    result = await agent._run_code(request)  # type: ignore[attr-defined]

    mcp.invoke.assert_awaited_once_with(
        "sandbox", "execute_code", {"language": "python", "code": "print(1 + 2)"}
    )
    assert "Exit code: 0" in result.text
    assert "3" in result.text


@pytest.mark.asyncio
async def test_run_code_surfaces_failure() -> None:
    mcp = AsyncMock()
    mcp.invoke.return_value = ToolInvocationResult(
        tool_name="execute_code",
        server_id="sandbox",
        success=False,
        output=None,
        error="container timeout",
    )
    agent = _coding_agent(mcp)
    request = MagicMock()
    request.raw_input = "while True: pass"

    result = await agent._run_code(request)  # type: ignore[attr-defined]
    assert "failed" in result.text.lower()
    assert "container timeout" in result.text


@pytest.mark.asyncio
async def test_run_code_without_mcp_client() -> None:
    agent = _coding_agent(None)
    result = await agent._run_code(MagicMock())  # type: ignore[attr-defined]
    assert "not available" in result.text
