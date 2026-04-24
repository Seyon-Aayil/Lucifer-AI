"""
master.mcp.transport.sse
=========================
Streamable HTTP/SSE MCP transport for remote integrations (Gmail, Notion, GitHub, Slack).
Connects to Dockerized MCP servers via HTTP POST with streaming response support.
"""
from __future__ import annotations

from typing import Any

import httpx

from master.core.logging import get_logger
from master.mcp.interfaces import MCPTransport, ToolSchema

log = get_logger(__name__)


class SSETransport(MCPTransport):
    """
    HTTP/SSE MCP transport client.
    MCP protocol: JSON-RPC 2.0 over HTTP POST with optional SSE streaming.
    """

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._timeout,
        )
        log.info("mcp.sse.connected", url=self._base_url)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        log.info("mcp.sse.disconnected", url=self._base_url)

    async def call_tool(self, tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("Transport not connected. Call connect() first.")

        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": input_data},
            "id": 1,
        }
        response = await self._client.post("/mcp", json=payload)
        response.raise_for_status()
        rpc_response: dict[str, Any] = response.json()

        if "error" in rpc_response:
            err = rpc_response["error"]
            raise RuntimeError(f"MCP server error [{err.get('code')}]: {err.get('message')}")

        result: dict[str, Any] = rpc_response.get("result", {})
        return result

    async def list_tools(self) -> list[ToolSchema]:
        if self._client is None:
            raise RuntimeError("Transport not connected.")

        payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1}
        response = await self._client.post("/mcp", json=payload)
        response.raise_for_status()
        rpc_response: dict[str, Any] = response.json()
        tools_raw: list[dict[str, Any]] = rpc_response.get("result", {}).get("tools", [])

        return [
            ToolSchema(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {"type": "object"}),
                requires_confirmation=t.get("requiresConfirmation", False),
            )
            for t in tools_raw
        ]
