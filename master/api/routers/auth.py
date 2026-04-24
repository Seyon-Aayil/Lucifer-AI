"""
master.api.routers.auth
========================
Authentication endpoints:
  POST /auth/device/register  → DeviceRegistrationRequest → TokenPair
  POST /auth/token/refresh    → RefreshRequest → TokenPair
  POST /auth/device/revoke    → RevokeRequest → 204

JWT issuance + single-use refresh token rotation via Postgres + Redis.
Device records persisted in `devices` table. Refresh token hashes in `refresh_tokens`.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis

from master.api.schemas import DeviceRegistrationRequest, RefreshRequest, RevokeRequest, TokenPair
from master.core.auth.jwt import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
    validate_access_token,
)
from master.core.auth.revocation import RevocationStore
from master.core.config import get_settings
from master.core.exceptions import AuthError, DeviceRevokedError, TokenExpiredError
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

router = APIRouter()
log = get_logger(__name__)
tracer = get_tracer(__name__)


def _get_db(request: Request) -> asyncpg.Pool:
    """FastAPI dependency: retrieve asyncpg connection pool from app.state."""
    return request.app.state.db_pool


def _get_redis(request: Request) -> Redis:
    """FastAPI dependency: retrieve async Redis client from app.state."""
    return request.app.state.redis


def _get_revocation(request: Request) -> RevocationStore:
    return RevocationStore(request.app.state.redis)


def _token_pair(device_id: str, refresh_raw: str) -> TokenPair:
    """Build a TokenPair response from device_id + raw refresh token."""
    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(device_id),
        refresh_token=refresh_raw,
        access_expires_in=settings.jwt_access_token_ttl_seconds,
        refresh_expires_in=settings.jwt_refresh_token_ttl_seconds,
    )


@router.post(
    "/device/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new device and receive token pair",
)
async def register_device(
    body: DeviceRegistrationRequest,
    db: asyncpg.Pool = Depends(_get_db),
) -> TokenPair:
    """
    Register a new edge device and issue a token pair.
    If device_id already exists, returns new tokens (re-registration).
    """
    with tracer.start_as_current_span("auth.register_device"):
        settings = get_settings()
        raw_refresh, refresh_hash = create_refresh_token()
        refresh_expires = datetime.now(UTC) + timedelta(seconds=settings.jwt_refresh_token_ttl_seconds)

        async with db.acquire() as conn:
            # Upsert device record
            await conn.execute(
                """
                INSERT INTO devices (device_id, device_type, name, app_version, os_version, last_seen_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (device_id) DO UPDATE
                  SET last_seen_at = NOW(), app_version = $4, os_version = $5
                """,
                body.device_id,
                body.device_type,
                body.name,
                body.app_version,
                body.os_version,
            )

            # Store refresh token hash (single-use)
            await conn.execute(
                """
                INSERT INTO refresh_tokens (device_id, token_hash, expires_at)
                VALUES ($1, $2, $3)
                """,
                body.device_id,
                refresh_hash,
                refresh_expires,
            )

        log.info("auth.device.registered", device_id=body.device_id, type=body.device_type)
        return _token_pair(body.device_id, raw_refresh)


@router.post(
    "/token/refresh",
    response_model=TokenPair,
    summary="Rotate refresh token and issue a new token pair",
)
async def refresh_token(
    body: RefreshRequest,
    db: asyncpg.Pool = Depends(_get_db),
    revocation: RevocationStore = Depends(_get_revocation),
) -> TokenPair:
    """
    Single-use refresh token rotation.
    The old refresh token is invalidated immediately; a new pair is returned.
    Raises 401 if the device is revoked, token is expired, or already used.
    """
    with tracer.start_as_current_span("auth.refresh_token"):
        token_hash = hash_refresh_token(body.refresh_token)
        settings = get_settings()

        if await revocation.is_device_revoked(body.device_id):
            raise HTTPException(status_code=401, detail={"error": "device_revoked"})

        async with db.acquire() as conn:
            record = await conn.fetchrow(
                """
                SELECT id, expires_at, used_at
                FROM refresh_tokens
                WHERE device_id = $1 AND token_hash = $2
                """,
                body.device_id,
                token_hash,
            )

            if record is None:
                raise HTTPException(status_code=401, detail={"error": "invalid_refresh_token"})

            if record["used_at"] is not None:
                # Token replay detected — revoke all device tokens
                await revocation.revoke_device(body.device_id)
                log.warning("auth.token_replay_detected", device_id=body.device_id)
                raise HTTPException(status_code=401, detail={"error": "token_replay_detected"})

            if datetime.now(UTC) > record["expires_at"].replace(tzinfo=UTC):
                raise HTTPException(status_code=401, detail={"error": "refresh_token_expired"})

            # Mark old token as used (single-use rotation)
            await conn.execute(
                "UPDATE refresh_tokens SET used_at = NOW() WHERE id = $1",
                record["id"],
            )

            # Issue new refresh token
            raw_refresh, new_hash = create_refresh_token()
            refresh_expires = datetime.now(UTC) + timedelta(seconds=settings.jwt_refresh_token_ttl_seconds)
            await conn.execute(
                "INSERT INTO refresh_tokens (device_id, token_hash, expires_at) VALUES ($1, $2, $3)",
                body.device_id,
                new_hash,
                refresh_expires,
            )

        log.info("auth.token.refreshed", device_id=body.device_id)
        return _token_pair(body.device_id, raw_refresh)


@router.post(
    "/device/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke all tokens for a device",
)
async def revoke_device(
    body: RevokeRequest,
    revocation: RevocationStore = Depends(_get_revocation),
    db: asyncpg.Pool = Depends(_get_db),
) -> None:
    """
    Revoke a device: marks it revoked in Postgres + propagates to Redis within 60s.
    All in-flight tokens for the device are rejected on the next request.
    """
    with tracer.start_as_current_span("auth.revoke_device"):
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE devices SET is_revoked = TRUE, revoked_at = NOW() WHERE device_id = $1",
                body.device_id,
            )
        await revocation.revoke_device(body.device_id)
        log.info("auth.device.revoked", device_id=body.device_id, reason=body.reason)
