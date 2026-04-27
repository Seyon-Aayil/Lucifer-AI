"""
master.agents.librarian.mem0_client
====================================
Client for the self-hosted Mem0 episodic memory engine.

Mem0 REST API (self-hosted, http://localhost:8090 by default):
  POST /v1/memories/            → add memories from messages
  POST /v1/memories/search/     → semantic search
  DELETE /v1/memories/{id}/     → delete a single memory

All operations are fire-and-forget safe: errors are logged but not raised
so a Mem0 outage never kills the orchestration pipeline.
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


class Mem0Client:
    """
    Async client for the self-hosted Mem0 episodic memory API.
    One shared instance per process — stateless, safe to reuse.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.mem0_api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Token {settings.mem0_api_key}",
            "Content-Type": "application/json",
        }

    # ── Read ─────────────────────────────────────────────────────────────────

    async def search(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Semantic search over episodic memories for a user.
        Returns a list of memory dicts with keys: id, memory, score, metadata.
        """
        with tracer.start_as_current_span("mem0.search"):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/v1/memories/search/",
                        headers=self._headers,
                        json={"query": query, "user_id": user_id, "limit": limit},
                    )
                    resp.raise_for_status()
                    results: list[dict[str, Any]] = resp.json().get("results", [])
                    log.debug("mem0.search.ok", user=user_id, count=len(results))
                    return results
            except Exception as exc:
                log.warning("mem0.search.failed", user=user_id, error=str(exc))
                return []

    # ── Write ─────────────────────────────────────────────────────────────────

    async def add(
        self,
        user_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """
        Ingest a list of chat messages as new episodic memories.
        Each message must have {"role": "user"|"assistant", "content": "..."}.
        Returns the IDs of the created memory records.
        """
        with tracer.start_as_current_span("mem0.add"):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self._base_url}/v1/memories/",
                        headers=self._headers,
                        json={
                            "messages": messages,
                            "user_id": user_id,
                            "metadata": metadata or {},
                        },
                    )
                    resp.raise_for_status()
                    ids = [m["id"] for m in resp.json().get("results", [])]
                    log.debug("mem0.add.ok", user=user_id, count=len(ids))
                    return ids
            except Exception as exc:
                log.warning("mem0.add.failed", user=user_id, error=str(exc))
                return []

    async def delete(self, memory_id: str) -> bool:
        """
        Hard-delete a single memory record.
        Returns True on success, False on failure.
        """
        with tracer.start_as_current_span("mem0.delete"):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.delete(
                        f"{self._base_url}/v1/memories/{memory_id}/",
                        headers=self._headers,
                    )
                    resp.raise_for_status()
                    log.debug("mem0.delete.ok", memory_id=memory_id)
                    return True
            except Exception as exc:
                log.warning("mem0.delete.failed", memory_id=memory_id, error=str(exc))
                return False
