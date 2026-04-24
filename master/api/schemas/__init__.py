"""
master.api.schemas
==================
Pydantic v2 request/response schemas for all API endpoints.
Schemas are strict — no extra fields allowed. All datetimes UTC.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Shared Base ──────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    """Base model: forbids extra fields, uses enum values."""
    model_config = ConfigDict(extra="forbid", use_enum_values=True, populate_by_name=True)


# ── Auth ─────────────────────────────────────────────────────────────────────

class DeviceType(str, Enum):
    DESKTOP_MAC = "desktop-mac"
    DESKTOP_WIN = "desktop-win"
    IOS = "ios"
    ANDROID = "android"
    WATCH_OS = "watchos"
    WEAR_OS = "wearos"
    WEB = "web"
    CLI = "cli"


class DeviceRegistrationRequest(StrictModel):
    """Request body for POST /auth/device/register."""
    device_id: str = Field(..., min_length=16, max_length=128,
                           description="Hashed device fingerprint (client-side derived)")
    device_type: DeviceType
    name: str | None = Field(None, max_length=64)
    app_version: str = Field(..., max_length=32)
    os_version: str | None = Field(None, max_length=64)
    public_key: str | None = Field(None, description="Ed25519 public key (DER, base64) for future mTLS")


class TokenPair(StrictModel):
    """Issued token pair returned on registration and refresh."""
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    access_expires_in: int = Field(..., description="Seconds until access token expires")
    refresh_expires_in: int = Field(..., description="Seconds until refresh token expires")


class RefreshRequest(StrictModel):
    """Request body for POST /auth/token/refresh."""
    refresh_token: str
    device_id: str


class RevokeRequest(StrictModel):
    """Request body for POST /auth/device/revoke."""
    device_id: str
    reason: str | None = None


# ── Chat ──────────────────────────────────────────────────────────────────────

class Surface(str, Enum):
    WATCH = "watch"
    MOBILE = "mobile"
    DESKTOP = "desktop"
    MASTER = "master"
    CLI = "cli"
    WEB = "web"


class ChatRequest(StrictModel):
    """Request body for POST /v1/chat."""
    message: str = Field(..., min_length=1, max_length=32_000)
    surface: Surface = Surface.WEB
    session_id: str | None = None
    device_id: str | None = None
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(StrictModel):
    """Synchronous chat response."""
    task_id: str
    agent_id: str
    content: str
    surface: Surface
    token_usage: TokenUsageSchema
    cost_usd: float | None
    latency_ms: int
    created_at: datetime


class ChatChunk(StrictModel):
    """Streaming chunk emitted via WebSocket or SSE."""
    task_id: str
    delta: str
    is_final: bool = False
    token_usage: TokenUsageSchema | None = None


class TokenUsageSchema(StrictModel):
    """Token consumption for a single LLM call or full agent turn."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int
    estimated_cost_usd: float | None = None


# ── Health ────────────────────────────────────────────────────────────────────

class ServiceStatus(StrictModel):
    status: str          # "ok" | "degraded" | "error" | "not_initialised"
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(StrictModel):
    status: str
    services: dict[str, ServiceStatus]
