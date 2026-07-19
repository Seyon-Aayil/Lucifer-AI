"""
master.mcp.client
==================
MCPClient: the single entry point for all MCP tool invocations.
Enforces: schema validation → agent manifest check → transport call → audit log.
Never call transport.call_tool() directly from agent code.
"""

from __future__ import annotations

import time
from typing import Any

import jsonschema

from master.api.middleware.content_policy import ContentPolicyValidator
from master.core.exceptions import (
    MCPPermissionError,
    MCPSchemaValidationError,
    MCPToolNotFoundError,
)
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.mcp.audit import AuditLogger
from master.mcp.interfaces import MCPServerConfig, MCPTransport, ToolInvocationResult, ToolSchema

log = get_logger(__name__)
tracer = get_tracer(__name__)


class AgentManifest:
    """
    Declares which MCP tools an agent is permitted to invoke.
    Loaded from agent_manifest.json at startup.
    """

    def __init__(self, agent_id: str, allowed_tools: dict[str, list[str]]) -> None:
        """
        Args:
            agent_id: The agent this manifest applies to.
            allowed_tools: {server_id: [tool_name, ...]} mapping.
        """
        self.agent_id = agent_id
        self._allowed: dict[str, set[str]] = {
            server: set(tools) for server, tools in allowed_tools.items()
        }

    def can_invoke(self, server_id: str, tool_name: str) -> bool:
        server_tools = self._allowed.get(server_id, set())
        return tool_name in server_tools or "*" in server_tools


class MCPClient:
    """
    Unified MCP tool invocation client.
    Instantiated per-agent; holds the agent's manifest for permission enforcement.

    Usage:
        result = await mcp_client.invoke("gmail", "read_emails", {"max_results": 10})
    """

    def __init__(
        self,
        agent_manifest: AgentManifest,
        transports: dict[str, MCPTransport],
        server_configs: dict[str, MCPServerConfig],
        audit_logger: AuditLogger,
        content_policy: ContentPolicyValidator | None = None,
        enforce_untrusted: bool = False,
    ) -> None:
        self._manifest = agent_manifest
        self._transports = transports
        self._configs = server_configs
        self._audit = audit_logger
        # W3-3: tool results are untrusted input. Reuse the content-policy
        # validator (it already carries the injection patterns — AGENTS.md rule
        # 4: reuse, don't add a parallel component). enforce=False is log-and-flag.
        self._content_policy = content_policy or ContentPolicyValidator()
        self._enforce_untrusted = enforce_untrusted
        # Tool schema cache: {server_id: {tool_name: ToolSchema}}
        self._tool_cache: dict[str, dict[str, ToolSchema]] = {}

    async def invoke(
        self,
        server_id: str,
        tool_name: str,
        input_data: dict[str, Any],
    ) -> ToolInvocationResult:
        """
        Invoke a tool after validating permissions and input schema.
        Records all calls to the HMAC audit log.

        Args:
            server_id: MCP server identifier (e.g. "gmail", "github").
            tool_name: Tool name as declared in the server manifest.
            input_data: Tool input; must match the tool's JSON Schema.

        Returns:
            ToolInvocationResult with output or error details.

        Raises:
            MCPPermissionError: Agent not allowed to call this tool.
            MCPToolNotFoundError: Tool not in server manifest.
            MCPSchemaValidationError: Input fails JSON Schema validation.
        """
        with tracer.start_as_current_span(f"mcp.invoke.{server_id}.{tool_name}"):
            # ── Permission Check (agent manifest) ────────────────────────────
            if not self._manifest.can_invoke(server_id, tool_name):
                raise MCPPermissionError(
                    f"Agent '{self._manifest.agent_id}' is not permitted to invoke "
                    f"'{tool_name}' on server '{server_id}'"
                )

            # ── Fetch + cache tool schema ─────────────────────────────────────
            tool_schema = await self._get_tool_schema(server_id, tool_name)

            # ── Schema Validation ─────────────────────────────────────────────
            self._validate_input(tool_schema, input_data)

            # ── Transport Call ────────────────────────────────────────────────
            transport = self._transports.get(server_id)
            if not transport:
                return ToolInvocationResult(
                    tool_name=tool_name,
                    server_id=server_id,
                    success=False,
                    output=None,
                    error=f"No transport registered for server '{server_id}'",
                )

            start = time.monotonic()
            try:
                raw_output = await transport.call_tool(tool_name, input_data)
                duration_ms = (time.monotonic() - start) * 1000

                # ── Untrusted-result scan (W3-3) ─────────────────────────────
                # A tool result is attacker-controllable (e.g. an email body or
                # Notion page carrying "IGNORE PREVIOUS INSTRUCTIONS…"). Scan it
                # before it can re-enter a prompt.
                output, flagged, flag_reason = self._screen_result(server_id, tool_name, raw_output)

                # ── Audit Log ────────────────────────────────────────────────
                seq = await self._audit.log_event(
                    event_type="mcp.tool_call",
                    action=f"{server_id}.{tool_name}",
                    resource=server_id,
                    payload={
                        "tool": tool_name,
                        "input_keys": list(input_data.keys()),
                        "flagged": flagged,
                    },
                    agent_id=self._manifest.agent_id,
                )

                log.info(
                    "mcp.tool.invoked",
                    server=server_id,
                    tool=tool_name,
                    agent=self._manifest.agent_id,
                    duration_ms=round(duration_ms, 1),
                    audit_seq=seq,
                    flagged=flagged,
                )

                return ToolInvocationResult(
                    tool_name=tool_name,
                    server_id=server_id,
                    success=True,
                    output=output,
                    duration_ms=duration_ms,
                    audit_sequence=seq,
                    flagged=flagged,
                    flag_reason=flag_reason,
                )

            except Exception as exc:
                duration_ms = (time.monotonic() - start) * 1000
                log.error("mcp.tool.failed", server=server_id, tool=tool_name, error=str(exc))

                await self._audit.log_event(
                    event_type="mcp.tool_call.error",
                    action=f"{server_id}.{tool_name}",
                    resource=server_id,
                    payload={"tool": tool_name, "error": str(exc)},
                    agent_id=self._manifest.agent_id,
                )

                return ToolInvocationResult(
                    tool_name=tool_name,
                    server_id=server_id,
                    success=False,
                    output=None,
                    error=str(exc),
                    duration_ms=duration_ms,
                )

    async def _get_tool_schema(self, server_id: str, tool_name: str) -> ToolSchema:
        if server_id not in self._tool_cache:
            transport = self._transports.get(server_id)
            if transport:
                tools = await transport.list_tools()
                self._tool_cache[server_id] = {t.name: t for t in tools}
            else:
                self._tool_cache[server_id] = {}

        schema = self._tool_cache.get(server_id, {}).get(tool_name)
        if schema is None:
            raise MCPToolNotFoundError(f"Tool '{tool_name}' not found on server '{server_id}'")
        return schema

    def _screen_result(
        self, server_id: str, tool_name: str, raw_output: Any
    ) -> tuple[Any, bool, str | None]:
        """
        Scan a tool result for prompt-injection (W3-3).

        Returns (output, flagged, flag_reason). In log-and-flag mode the original
        output is returned unchanged with flagged=True. In enforce mode the
        offending content is replaced with a neutral placeholder so it can never
        re-enter a prompt. Reuses ContentPolicyValidator's injection patterns.
        """
        result = self._content_policy.validate_input(str(raw_output))
        if result.allowed:
            return raw_output, False, None

        reason = result.violation or "untrusted tool result flagged"
        log.warning(
            "mcp.tool.untrusted_flagged",
            server=server_id,
            tool=tool_name,
            check=result.check,
            enforce=self._enforce_untrusted,
        )
        if self._enforce_untrusted:
            neutral = {
                "error": "tool_result_blocked",
                "reason": f"{server_id}.{tool_name} result blocked: {reason}",
            }
            return neutral, True, reason
        return raw_output, True, reason

    def _validate_input(self, tool_schema: ToolSchema, input_data: dict[str, Any]) -> None:
        try:
            jsonschema.validate(instance=input_data, schema=tool_schema.input_schema)
        except jsonschema.ValidationError as exc:
            raise MCPSchemaValidationError(
                f"Input validation failed for tool '{tool_schema.name}': {exc.message}"
            ) from exc
