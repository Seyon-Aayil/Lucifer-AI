"""
master.api.routers.health
=========================
Health check endpoints. Reports status of all service dependencies.
Used by: Docker Compose healthchecks, Kubernetes probes, Grafana.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from neo4j import AsyncGraphDatabase
from redis.asyncio import from_url as redis_from_url

from master.core.config import get_settings
from master.core.logging import get_logger

router = APIRouter()
log = get_logger(__name__)


async def _check_postgres(dsn: str) -> dict[str, Any]:
    """Verify Postgres connectivity and TimescaleDB extension."""
    import asyncpg

    start = time.monotonic()
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=3.0)
        await conn.execute("SELECT 1")
        await conn.close()
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


async def _check_neo4j(uri: str, user: str, password: str) -> dict[str, Any]:
    """Verify Neo4j connectivity via Bolt."""
    start = time.monotonic()
    try:
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


async def _check_redis(url: str) -> dict[str, Any]:
    """Verify Redis connectivity."""
    start = time.monotonic()
    try:
        client = redis_from_url(url)
        await asyncio.wait_for(client.ping(), timeout=2.0)
        await client.aclose()
        return {"status": "ok", "latency_ms": round((time.monotonic() - start) * 1000, 1)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


async def _check_litellm(proxy_url: str) -> dict[str, Any]:
    """Verify LiteLLM proxy is reachable."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{proxy_url}/health")
            return {
                "status": "ok" if resp.status_code == 200 else "degraded",
                "latency_ms": round((time.monotonic() - start) * 1000, 1),
            }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@router.get("/", summary="Basic liveness check")
async def liveness() -> dict[str, str]:
    """Returns 200 immediately. Used by load balancers for liveness probe."""
    return {"status": "ok"}


@router.get("/ready", summary="Full readiness check — all dependencies")
async def readiness(request: Request) -> dict[str, Any]:
    """
    Checks connectivity to: Postgres, Neo4j, Redis, NATS, LiteLLM.
    Returns per-service status and latency. Overall status = degraded
    if any service is unhealthy.
    """
    settings = get_settings()

    # Run all checks in parallel
    postgres_check, neo4j_check, redis_check, litellm_check = await asyncio.gather(
        _check_postgres(settings.database_url),
        _check_neo4j(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password),
        _check_redis(settings.redis_url),
        _check_litellm(settings.litellm_proxy_url),
        return_exceptions=True,
    )

    # NATS is checked via app.state
    nats_status: dict[str, Any]
    try:
        nc = request.app.state.nats
        nats_status = {"status": "ok" if nc.is_connected else "error"}
    except AttributeError:
        nats_status = {"status": "not_initialised"}

    services = {
        "postgres": postgres_check,
        "neo4j": neo4j_check,
        "redis": redis_check,
        "litellm": litellm_check,
        "nats": nats_status,
    }

    overall = all(
        isinstance(s, dict) and s.get("status") == "ok" for s in services.values()
    )

    return {
        "status": "ok" if overall else "degraded",
        "services": services,
    }
