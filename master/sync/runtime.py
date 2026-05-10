"""
master.sync.runtime
====================
Starts and stops the gRPC LuciferSync server.

mTLS: if all three TLS paths are configured in Settings, a secure channel
with mutual TLS is used. Otherwise the server binds insecurely (dev / test only).

Usage in FastAPI lifespan:
    grpc_server = await start_grpc_server(app, settings)
    ...
    await grpc_server.stop(grace=5)
"""
from __future__ import annotations

import asyncio
from typing import Any

import grpc
import grpc.aio

from master.core.config import Settings
from master.core.logging import get_logger
from master.sync.auth import DeviceAuthInterceptor
from master.sync.lucifer_sync_pb2_grpc import add_LuciferSyncServicer_to_server  # type: ignore[import]
from master.sync.server import LuciferSyncServicer

log = get_logger(__name__)


class GRPCServer:
    """Thin wrapper around grpc.aio.Server for start/stop control."""

    def __init__(self, server: grpc.aio.Server, bind_addr: str) -> None:
        self._server = server
        self._addr = bind_addr
        self._running = False

    async def start(self) -> None:
        await self._server.start()
        self._running = True
        log.info("grpc.server.started", addr=self._addr)

    async def stop(self, grace: float = 5.0) -> None:
        await self._server.stop(grace=grace)
        self._running = False
        log.info("grpc.server.stopped")

    def is_running(self) -> bool:
        return self._running


async def start_grpc_server(
    graph_client: Any,
    db_pool: Any,
    nats_js: Any,
    audit_logger: Any,
    revocation_store: Any,
    settings: Settings,
) -> GRPCServer:
    """
    Build and start the gRPC server. Returns a GRPCServer handle.
    Called from FastAPI lifespan; the handle is stored in app.state.
    """
    interceptor = DeviceAuthInterceptor(revocation_store)
    server = grpc.aio.server(interceptors=[interceptor])

    servicer = LuciferSyncServicer(
        graph_client=graph_client,
        db_pool=db_pool,
        nats_js=nats_js,
        audit_logger=audit_logger,
    )
    add_LuciferSyncServicer_to_server(servicer, server)

    credentials = _load_credentials(settings)
    if credentials:
        server.add_secure_port(settings.grpc_bind_addr, credentials)
        log.info("grpc.tls.enabled", addr=settings.grpc_bind_addr)
    else:
        server.add_insecure_port(settings.grpc_bind_addr)
        log.warning("grpc.tls.disabled", addr=settings.grpc_bind_addr)

    wrapped = GRPCServer(server, settings.grpc_bind_addr)
    await wrapped.start()
    return wrapped


def _load_credentials(settings: Settings) -> grpc.ServerCredentials | None:
    """Return mTLS ServerCredentials if all TLS paths are configured."""
    if not (settings.grpc_tls_cert_path and settings.grpc_tls_key_path and settings.grpc_tls_client_ca_path):
        return None
    try:
        with open(settings.grpc_tls_cert_path, "rb") as f:
            cert = f.read()
        with open(settings.grpc_tls_key_path, "rb") as f:
            key = f.read()
        with open(settings.grpc_tls_client_ca_path, "rb") as f:
            ca = f.read()
        return grpc.ssl_server_credentials(
            [(key, cert)],
            root_certificates=ca,
            require_client_auth=True,
        )
    except OSError as exc:
        log.error("grpc.tls.load_failed", error=str(exc))
        return None
