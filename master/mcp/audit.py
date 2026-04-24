"""
master.mcp.audit
=================
HMAC-chained audit log for all MCP tool calls and agent executions.
Every record appends to an immutable chain stored in Postgres `audit_log`.
Chain integrity can be verified offline via `audit_verifier.py` (Phase 6).
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from master.core.config import get_settings
from master.core.crypto import compute_chain_hmac, sha256_hex
from master.core.logging import get_logger

log = get_logger(__name__)


class AuditLogger:
    """
    Append-only HMAC audit log writer.
    One shared instance per application process.
    Thread-safe via Postgres sequence (BIGSERIAL) as the chain anchor.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self._pool = db_pool
        self._secret = get_settings().app_secret_key.encode()

    async def log_event(
        self,
        event_type: str,
        action: str,
        resource: str,
        payload: dict[str, Any],
        agent_id: str | None = None,
        device_id: str | None = None,
    ) -> int:
        """
        Append a new record to the HMAC audit chain.
        Returns the sequence number of the appended record.
        Never raises — audit failures are logged but swallowed
        to avoid blocking agent execution.
        """
        try:
            payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode()
            payload_hash = sha256_hex(payload_bytes)

            async with self._pool.acquire() as conn:
                # Get the last chain HMAC (for chain continuity)
                last = await conn.fetchval(
                    "SELECT chain_hmac FROM audit_log ORDER BY sequence DESC LIMIT 1"
                )
                prev_hmac = last if last else "0" * 64

                chain_hmac = compute_chain_hmac(prev_hmac, payload_hash, self._secret)

                seq: int = await conn.fetchval(
                    """
                    INSERT INTO audit_log
                        (event_type, agent_id, device_id, resource, action,
                         payload_hash, prev_hmac, chain_hmac, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING sequence
                    """,
                    event_type,
                    agent_id,
                    device_id,
                    resource,
                    action,
                    payload_hash,
                    prev_hmac,
                    chain_hmac,
                    json.dumps(payload, default=str),
                )

            log.debug(
                "audit.logged",
                sequence=seq,
                event_type=event_type,
                agent=agent_id,
                action=action,
            )
            return seq

        except Exception as exc:
            log.error("audit.log_failed", error=str(exc), event_type=event_type)
            return -1
