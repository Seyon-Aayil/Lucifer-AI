"""
master.api.main
===============
FastAPI application factory and lifespan context manager.
Initialises all infrastructure connections at startup and tears them down
cleanly on shutdown. All routers registered here.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
import nats
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from redis.asyncio import Redis

from master.api.middleware.auth import AuthMiddleware
from master.api.routers import auth, chat, health
from master.core.auth.revocation import RevocationStore
from master.core.config import get_settings
from master.core.logging import get_logger, setup_logging
from master.core.telemetry import setup_telemetry
from master.mcp.audit import AuditLogger
from master.mcp.registry import MCPServerRegistry
from master.token_optimizer.spend_tracker import SpendTracker

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan: connect to all services on startup,
    close cleanly on shutdown. All connections stored in app.state.
    """
    settings = get_settings()
    log.info("lucifer.startup", env=settings.app_env.value)

    # ── Postgres connection pool ─────────────────────────────────────────────
    db_pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=20)
    app.state.db_pool = db_pool
    log.info("postgres.connected")

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_client = Redis.from_url(settings.redis_url)
    app.state.redis = redis_client
    log.info("redis.connected")

    # ── SpendTracker (budget hardening) ─────────────────────────────────────
    spend_tracker = SpendTracker(redis=redis_client, db_pool=db_pool)
    await spend_tracker.load_limits()
    app.state.spend_tracker = spend_tracker
    log.info("spend_tracker.ready")

    # ── MCP Registry ─────────────────────────────────────────────────────────
    audit_logger = AuditLogger(db_pool)
    mcp_registry = MCPServerRegistry.from_config(audit_logger)
    await mcp_registry.connect_all()
    app.state.mcp_registry = mcp_registry
    log.info("mcp_registry.ready")

    # ── NATS JetStream ───────────────────────────────────────────────────────
    nc = await nats.connect(settings.nats_url)
    js = nc.jetstream()
    app.state.nats = nc
    app.state.js = js
    log.info("nats.connected", url=settings.nats_url)

    # ── Create JetStream streams (idempotent) ────────────────────────────────
    stream_subjects = {
        "lucifer-agents":    ["agent.task.>"],
        "lucifer-memory":    ["memory.delta.>"],
        "lucifer-telemetry": ["telemetry.event.>"],
        "lucifer-news":      ["news.fetch.>"],
        "lucifer-sync":      ["sync.edge.>"],
        "lucifer-alerts":    ["alert.>"],
    }
    for stream_name, subjects in stream_subjects.items():
        try:
            await js.add_stream(name=stream_name, subjects=subjects)
        except Exception:
            pass  # Stream already exists
    log.info("nats.streams_ready")

    yield  # ── Application is running ────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("lucifer.shutdown")
    await mcp_registry.disconnect_all()
    await nc.drain()
    await db_pool.close()
    await redis_client.aclose()
    log.info("lucifer.shutdown.complete")



def create_app() -> FastAPI:
    """
    Application factory. Creates and configures the FastAPI instance.
    Use this in tests and production entry-point (uvicorn).
    """
    settings = get_settings()

    # Logging + Telemetry must be initialised before the app is created
    setup_logging()
    setup_telemetry()

    app = FastAPI(
        title="Lucifer AI — Master API",
        description="Distributed, privacy-first personal AI OS",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── CORS ────────────────────────────────────────────────────────────────
    origins = (
        ["http://localhost:3000", "http://localhost:5173"]
        if settings.is_development
        else []
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    redis_client = Redis.from_url(settings.redis_url)
    app.add_middleware(
        AuthMiddleware,
        revocation_store=RevocationStore(redis_client)
    )

    # ── Routers ─────────────────────────────────────────────────────────────
    app.include_router(health.router, prefix="/health", tags=["health"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(chat.router, prefix="/v1", tags=["chat"])

    # ── OpenTelemetry Auto-instrumentation ──────────────────────────────────
    FastAPIInstrumentor.instrument_app(app)

    return app


# Entry point for `uvicorn master.api.main:app`
app = create_app()
