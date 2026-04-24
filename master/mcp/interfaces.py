"""
master.mcp.interfaces
======================
MCP (Model Context Protocol) abstract interfaces and data types.
All MCP tool calls go through MCPClient.invoke() — never called directly.
Schema validation, HMAC audit logging, and sandbox isolation are enforced here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TransportType(str, Enum):
    STDIO = "stdio"           # Local process, stdio-based
    SSE = "sse"               # Streamable HTTP/SSE
    GRPC = "grpc"             # Internal gRPC (future)


class AuthMethod(str, Enum):
    NONE = "none"
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    DOCKER_INTERNAL = "docker_internal"


@dataclass(frozen=True)
class ToolSchema:
    """JSON Schema spec for a single MCP tool."""
    name: str
    description: str
    input_schema: dict[str, Any]           # JSON Schema object
    output_schema: dict[str, Any] | None = None
    requires_confirmation: bool = False    # If True, surfaces a HitL checkpoint


@dataclass
class MCPServerConfig:
    """
    Configuration for a single MCP server, loaded from mcp_servers.yaml.
    """
    server_id: str
    display_name: str
    transport: TransportType
    auth_method: AuthMethod
    command: list[str] | None = None       # stdio: process command
    url: str | None = None                 # sse: base URL
    docker_image: str | None = None        # Docker image for sandboxed servers
    allowed_agents: list[str] = field(default_factory=list)
    tools: list[ToolSchema] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolInvocationResult:
    """Result from a single MCP tool call."""
    tool_name: str
    server_id: str
    success: bool
    output: Any                            # Parsed tool output
    error: str | None = None
    duration_ms: float = 0.0
    audit_sequence: int = 0               # HMAC audit log sequence number


class MCPTransport(ABC):
    """Abstract transport layer for MCP communication."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the MCP server."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection cleanly."""

    @abstractmethod
    async def call_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Invoke a tool on the server and return the raw output dict.
        Schema validation happens in MCPClient before this is called.
        """

    @abstractmethod
    async def list_tools(self) -> list[ToolSchema]:
        """Fetch the tool manifest from the server."""
