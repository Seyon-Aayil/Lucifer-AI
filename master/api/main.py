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

import nats
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from master.api.routers import auth, chat, health
from master.core.config import get_settings
from master.core.logging import get_logger, setup_logging
from master.core.telemetry import setup_telemetry

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan: connect to all services on startup,
    close cleanly on shutdown. All connections stored in app.state.
    """
    settings = get_settings()
    log.info("lucifer.startup", env=settings.app_env.value)

    # ── NATS JetStream ──────────────────────────────────────────────────────
    nc = await nats.connect(settings.nats_url)
    js = nc.jetstream()
    app.state.nats = nc
    app.state.js = js
    log.info("nats.connected", url=settings.nats_url)

    # ── Create JetStream streams (idempotent) ───────────────────────────────
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

    # ── Shutdown ─────────────────────────────────────────────────────────────
    log.info("lucifer.shutdown")
    await nc.drain()
    log.info("nats.disconnected")


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

    # ── Routers ─────────────────────────────────────────────────────────────
    app.include_router(health.router, prefix="/health", tags=["health"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(chat.router, prefix="/v1", tags=["chat"])

    # ── OpenTelemetry Auto-instrumentation ──────────────────────────────────
    FastAPIInstrumentor.instrument_app(app)

    return app


# Entry point for `uvicorn master.api.main:app`
app = create_app()
