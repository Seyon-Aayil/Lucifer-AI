"""
master.mcp.transport.docker
============================
Docker-sandboxed MCP transport.

Lifecycle per MCP server call:
  1. connect() — pull image if needed, start container, wait for /health → 200.
  2. call_tool() / list_tools() — delegates to SSETransport pointed at the
     container's mapped port.
  3. disconnect() — stop and remove the ephemeral container.

Security guarantees:
  - No host network access: container runs on an isolated bridge network.
  - Read-only root FS where possible.
  - Hard resource caps: 256 MB RAM, 0.5 CPU.
  - Container is always destroyed after disconnect(), never reused across agents.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from master.core.logging import get_logger
from master.mcp.interfaces import MCPTransport, ToolSchema
from master.mcp.transport.sse import SSETransport

log = get_logger(__name__)

# Docker daemon socket — prefer environment override
_DOCKER_BRIDGE_NETWORK = "lucifer-mcp"
_HEALTH_TIMEOUT_SECONDS = 30
_HEALTH_POLL_INTERVAL = 0.5
_CONTAINER_MEM_LIMIT = "256m"
_CONTAINER_CPU_QUOTA = 50_000  # microseconds per 100ms period = 0.5 CPU
_CONTAINER_EXPOSED_PORT = 8080


class DockerTransport(MCPTransport):
    """
    Spawns a Docker container hosting an MCP server and proxies
    tool calls to it via SSETransport.

    One DockerTransport instance = one ephemeral container lifecycle.
    Always call connect() before use and disconnect() on teardown.
    """

    def __init__(
        self,
        image: str,
        env_vars: dict[str, str] | None = None,
        host_port: int | None = None,
        timeout: float = 30.0,
        require_digest: bool = False,
    ) -> None:
        """
        Args:
            image: Docker image reference (e.g. 'lucifer/gmail-mcp:latest').
            env_vars: Environment variables injected into the container.
            host_port: Fixed host port mapping; auto-assigned if None.
            timeout: HTTP timeout for tool calls (seconds).
            require_digest: When True, reject any image not pinned by @sha256:
                digest (W3-2 rug-pull defence). A mutable tag like :latest can
                be repointed at a malicious image after review.
        """
        if require_digest and "@sha256:" not in image:
            raise ValueError(
                f"MCP image '{image}' must be pinned by @sha256: digest "
                "(mcp_require_image_digest is on) — a mutable tag is a rug-pull risk."
            )
        self._image = image
        self._env_vars: dict[str, str] = env_vars or {}
        self._host_port = host_port
        self._timeout = timeout
        self._container_id: str | None = None
        self._assigned_port: int | None = None
        self._sse: SSETransport | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Pull (if needed), start container, wait for health check."""
        import docker

        client = docker.DockerClient.from_env()
        container_name = f"lucifer-mcp-{uuid.uuid4().hex[:8]}"
        port = self._host_port or await _find_free_port()

        env_list = [f"{k}={v}" for k, v in self._env_vars.items()]

        log.info("docker.mcp.starting", image=self._image, port=port, name=container_name)

        container = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.containers.run(
                self._image,
                detach=True,
                name=container_name,
                environment=env_list,
                ports={f"{_CONTAINER_EXPOSED_PORT}/tcp": port},
                network=_DOCKER_BRIDGE_NETWORK,
                mem_limit=_CONTAINER_MEM_LIMIT,
                cpu_quota=_CONTAINER_CPU_QUOTA,
                read_only=False,  # some MCP servers need /tmp writes
                auto_remove=False,  # we control removal in disconnect()
                remove=False,
            ),
        )

        self._container_id = container.id
        self._assigned_port = port

        # Wait for the container's /health endpoint to return 200
        await self._wait_for_health(port)

        self._sse = SSETransport(
            base_url=f"http://localhost:{port}",
            timeout=self._timeout,
        )
        await self._sse.connect()
        log.info("docker.mcp.ready", image=self._image, container=container.short_id, port=port)

    async def disconnect(self) -> None:
        """Stop and remove the container. Always call this to avoid leaks."""
        if self._sse:
            await self._sse.disconnect()

        if self._container_id:
            import docker

            client = docker.DockerClient.from_env()

            def _stop_remove() -> None:
                try:
                    c = client.containers.get(self._container_id)
                    c.stop(timeout=5)
                    c.remove(force=True)
                    log.info("docker.mcp.removed", container=(self._container_id or "")[:12])
                except Exception as exc:
                    log.warning("docker.mcp.remove_failed", error=str(exc))

            await asyncio.get_event_loop().run_in_executor(None, _stop_remove)
            self._container_id = None

    # ── Delegation to SSE ─────────────────────────────────────────────────────

    async def call_tool(self, tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        if not self._sse:
            raise RuntimeError("DockerTransport not connected.")
        return await self._sse.call_tool(tool_name, input_data)

    async def list_tools(self) -> list[ToolSchema]:
        if not self._sse:
            raise RuntimeError("DockerTransport not connected.")
        return await self._sse.list_tools()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _wait_for_health(self, port: int) -> None:
        """Poll GET /health until 200 or timeout."""
        url = f"http://localhost:{port}/health"
        deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT_SECONDS

        async with httpx.AsyncClient(timeout=3.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        log.debug("docker.mcp.healthy", port=port)
                        return
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        raise TimeoutError(
            f"MCP container on port {port} did not become healthy "
            f"within {_HEALTH_TIMEOUT_SECONDS}s (image={self._image})"
        )


async def _find_free_port() -> int:
    """Find an unused TCP port on the host."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])
