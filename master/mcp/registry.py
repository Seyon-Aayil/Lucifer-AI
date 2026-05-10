"""
master.mcp.registry
====================
MCPServerRegistry: runtime registry of all configured MCP servers.

Responsibilities:
  - Loads server config from `infra/mcp_servers.yaml` at startup.
  - Creates the correct transport (DockerTransport or SSETransport) per server.
  - Vends a fully-wired MCPClient for a given agent (using that agent's manifest).
  - Manages transport lifecycle (connect/disconnect).

Usage:
    registry = MCPServerRegistry.from_config()
    await registry.connect_all()
    client = registry.get_client(agent_manifest)
    result = await client.invoke("gmail", "read_emails", {...})
    await registry.disconnect_all()
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from master.core.config import get_settings
from master.core.logging import get_logger
from master.mcp.audit import AuditLogger
from master.mcp.client import AgentManifest, MCPClient
from master.mcp.interfaces import AuthMethod, MCPServerConfig, MCPTransport, TransportType
from master.mcp.transport.sse import SSETransport

log = get_logger(__name__)

# Config path relative to repo root
_DEFAULT_CONFIG_PATH = Path(__file__).parents[3] / "infra" / "mcp_servers.yaml"
# Agent manifests path
_MANIFESTS_DIR = Path(__file__).parents[2] / "agents"


class MCPServerRegistry:
    """
    Central registry for all MCP server transports.
    Instantiated once per application process in the FastAPI lifespan.
    """

    def __init__(
        self,
        server_configs: dict[str, MCPServerConfig],
        audit_logger: AuditLogger,
    ) -> None:
        self._configs = server_configs
        self._audit = audit_logger
        self._transports: dict[str, MCPTransport] = {}

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls, audit_logger: AuditLogger, config_path: Path | None = None
    ) -> MCPServerRegistry:
        """Load server configs from YAML and construct the registry."""
        path = config_path or _DEFAULT_CONFIG_PATH
        if not path.exists():
            log.warning("mcp.registry.config_missing", path=str(path))
            return cls({}, audit_logger)

        with path.open() as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        configs: dict[str, MCPServerConfig] = {}
        servers_raw = raw.get("servers", [])
        # Support both list format [{server_id: ...}] and dict format {id: {...}}
        if isinstance(servers_raw, list):
            items = [(s["server_id"], s) for s in servers_raw]
        else:
            items = list(servers_raw.items())

        for server_id, cfg in items:
            configs[server_id] = MCPServerConfig(
                server_id=server_id,
                display_name=cfg.get("display_name", server_id),
                transport=TransportType(cfg.get("transport", "sse")),
                auth_method=AuthMethod(cfg.get("auth_method", "none")),
                command=cfg.get("command"),
                url=cfg.get("url"),
                docker_image=cfg.get("docker_image"),
                allowed_agents=cfg.get("allowed_agents", []),
                env_vars=cfg.get("env_vars", {}),
            )
            log.debug(
                "mcp.registry.server_loaded", server_id=server_id, transport=cfg.get("transport")
            )

        log.info("mcp.registry.loaded", count=len(configs))
        return cls(configs, audit_logger)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Establish connections to all configured MCP servers."""
        for server_id, config in self._configs.items():
            try:
                transport = self._build_transport(config)
                await transport.connect()
                self._transports[server_id] = transport
                log.info("mcp.registry.connected", server_id=server_id)
            except Exception as exc:
                log.error("mcp.registry.connect_failed", server_id=server_id, error=str(exc))

    async def disconnect_all(self) -> None:
        """Disconnect and clean up all transports."""
        for server_id, transport in self._transports.items():
            try:
                await transport.disconnect()
                log.info("mcp.registry.disconnected", server_id=server_id)
            except Exception as exc:
                log.error("mcp.registry.disconnect_failed", server_id=server_id, error=str(exc))
        self._transports.clear()

    async def connect_server(self, server_id: str) -> None:
        """Connect a single server on demand (lazy connection)."""
        if server_id in self._transports:
            return
        config = self._configs.get(server_id)
        if not config:
            raise KeyError(f"MCP server '{server_id}' not found in registry.")
        transport = self._build_transport(config)
        await transport.connect()
        self._transports[server_id] = transport

    # ── Client Vending ────────────────────────────────────────────────────────

    def get_client(self, manifest: AgentManifest) -> MCPClient:
        """
        Return an MCPClient wired to this registry's transports
        and restricted to the given agent manifest's permissions.
        """
        return MCPClient(
            agent_manifest=manifest,
            transports=self._transports,
            server_configs=self._configs,
            audit_logger=self._audit,
        )

    @staticmethod
    def load_manifest(agent_id: str) -> AgentManifest:
        """
        Load an agent's manifest from `agents/<type>/agent_manifest.json`.
        Raises FileNotFoundError if not present.
        """
        # Normalize agent_id to directory name (e.g. "personal-agent" → "personal")
        dir_name = agent_id.removesuffix("-agent")
        manifest_path = _MANIFESTS_DIR / dir_name / "agent_manifest.json"
        if not manifest_path.exists():
            log.warning("mcp.registry.manifest_missing", agent_id=agent_id, path=str(manifest_path))
            return AgentManifest(agent_id=agent_id, allowed_tools={})

        with manifest_path.open() as f:
            data: dict[str, Any] = json.load(f)

        return AgentManifest(
            agent_id=agent_id,
            allowed_tools=data.get("allowed_tools", {}),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_transport(self, config: MCPServerConfig) -> MCPTransport:
        """Construct the right transport for the server config."""
        settings = get_settings()

        if config.transport == TransportType.SSE:
            if not config.url:
                raise ValueError(f"SSE transport requires 'url' for server '{config.server_id}'")
            api_key = config.env_vars.get("MCP_API_KEY") or getattr(
                settings, f"mcp_{config.server_id}_api_key", None
            )
            return SSETransport(base_url=config.url, api_key=api_key)

        if config.transport == TransportType.STDIO:
            # Docker sandbox wraps stdio-style servers as well
            if config.docker_image:
                from master.mcp.transport.docker import DockerTransport

                return DockerTransport(
                    image=config.docker_image,
                    env_vars=self._resolve_env(config.env_vars),
                )
            raise ValueError(
                f"STDIO transport without docker_image is not permitted for '{config.server_id}'"
            )

        raise ValueError(
            f"Unsupported transport type '{config.transport}' for '{config.server_id}'"
        )

    def _resolve_env(self, raw_env: dict[str, str]) -> dict[str, str]:
        """
        Resolve ${SETTING_NAME} placeholders in env_vars values
        from application settings.
        """
        settings = get_settings()
        resolved: dict[str, str] = {}
        for k, v in raw_env.items():
            if v.startswith("${") and v.endswith("}"):
                attr = v[2:-1].lower()
                resolved[k] = str(getattr(settings, attr, v))
            else:
                resolved[k] = v
        return resolved
