"""
master.agents.librarian.zep_client
===================================
Client for the self-hosted Zep / Graphiti temporal memory engine.

Zep v2 REST API (self-hosted, http://localhost:8091 by default):
  POST /api/v2/sessions                      → create / get session
  POST /api/v2/sessions/{id}/memory          → add episode (messages)
  POST /api/v2/sessions/{id}/search          → semantic search over memory

Sessions map 1-to-1 with users. Each user has a single long-lived session
that accumulates timestamped episodes. Zep's Graphiti engine derives
temporal facts and entity relationships from the episode stream.

All operations degrade gracefully: errors are logged but never raised.
"""

from __future__ import annotations

from typing import Any

import httpx

from master.core.config import get_settings
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)

_TIMEOUT = 5.0  # seconds


class ZepClient:
    """
    Async client for the self-hosted Zep temporal memory API.
    One shared instance per process — stateless, safe to reuse.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.zep_api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Api-Key {settings.zep_api_key}",
            "Content-Type": "application/json",
        }

    # ── Session management ────────────────────────────────────────────────────

    async def ensure_session(self, user_id: str) -> None:
        """
        Create a Zep session for the user if one does not already exist.
        Safe to call multiple times (idempotent).
        """
        with tracer.start_as_current_span("zep.ensure_session"):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/v2/sessions",
                        headers=self._headers,
                        json={"session_id": user_id, "user_id": user_id},
                    )
                    # 200/201 = created, 409 = already exists — both are fine
                    if resp.status_code not in (200, 201, 409):
                        resp.raise_for_status()
                    log.debug("zep.session.ensured", user=user_id)
            except Exception as exc:
                log.warning("zep.ensure_session.failed", user=user_id, error=str(exc))

    # ── Read ─────────────────────────────────────────────────────────────────

    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Search the temporal memory graph for facts relevant to the query.
        Returns a list of result dicts with keys: uuid, content, score, metadata.
        """
        with tracer.start_as_current_span("zep.search"):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/v2/sessions/{user_id}/search",
                        headers=self._headers,
                        json={"text": query, "limit": limit},
                    )
                    resp.raise_for_status()
                    results: list[dict[str, Any]] = resp.json().get("results", [])
                    log.debug("zep.search.ok", user=user_id, count=len(results))
                    return results
            except Exception as exc:
                log.warning("zep.search.failed", user=user_id, error=str(exc))
                return []

    # ── Write ─────────────────────────────────────────────────────────────────

    async def add_episode(
        self,
        user_id: str,
        messages: list[dict[str, str]],
    ) -> bool:
        """
        Add a list of messages as a new episode to the user's temporal memory.
        Each message must have {"role": "human"|"ai"|"system", "content": "..."}.
        Zep will extract facts and entities from the episode automatically.
        Returns True on success, False on failure.
        """
        with tracer.start_as_current_span("zep.add_episode"):
            await self.ensure_session(user_id)
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/v2/sessions/{user_id}/memory",
                        headers=self._headers,
                        json={"messages": messages},
                    )
                    resp.raise_for_status()
                    log.debug("zep.add_episode.ok", user=user_id, messages=len(messages))
                    return True
            except Exception as exc:
                log.warning("zep.add_episode.failed", user=user_id, error=str(exc))
                return False
