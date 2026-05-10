"""
master.api.routers.devices
===========================
Device pairing endpoints.

The flow:
  1. Operator (logged into the master web admin) calls
     `POST /devices/admin/code` to mint a fresh 6-digit code (60 s TTL).
  2. They show the code to the new device (QR or read-aloud).
  3. The device calls `POST /devices/pair` with the code + a stable
     `device_id`. The master verifies the code, mints a client cert and an
     access JWT, and returns the bundle.
  4. The device persists the cert + key in its OS keychain and uses them
     to open mTLS gRPC sessions thereafter.

Both endpoints are exempted from JWT auth in
`master.api.middleware.auth.EXEMPT_PATHS`. The admin endpoint is gated
behind a separate operator-only credential check (`require_admin`) — for
now this is a shared bearer token from settings; production should
replace it with proper SSO/operator login.
"""

from __future__ import annotations

import base64
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis

from master.core.auth.cert_mint import mint_client_credentials
from master.core.auth.device_pairing import PairingCodeStore
from master.core.auth.jwt import create_access_token
from master.core.auth.rate_limit import FixedWindowRateLimiter
from master.core.auth.revocation import RevocationStore
from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


# ── DTOs ─────────────────────────────────────────────────────────────────────


class MintCodeRequest(BaseModel):
    issued_for: str | None = Field(
        default=None,
        max_length=128,
        description="Optional human-readable hint shown to the operator (e.g. 'macbook-pro').",
    )


class MintCodeResponse(BaseModel):
    code: str
    ttl_seconds: int
    issued_for: str | None


class PairRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    device_id: str = Field(..., min_length=1, max_length=128)
    fingerprint: str | None = Field(
        default=None,
        max_length=256,
        description="Optional device hardware fingerprint (TPM / Secure Enclave).",
    )

    @field_validator("code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("code must be 6 digits")
        return v

    @field_validator("device_id")
    @classmethod
    def _safe_device_id(cls, v: str) -> str:
        # Keep the id printable + URL-safe so it can land in cert CN / metadata.
        if not all(c.isalnum() or c in "-_." for c in v):
            raise ValueError("device_id may only contain alphanumerics, '-', '_', '.'")
        return v


class RevokeRequest(BaseModel):
    cert_serial_hex: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Hex serial number returned by /devices/pair (with or without leading 0x).",
    )
    device_id: str | None = Field(default=None, max_length=128)


class RevokeResponse(BaseModel):
    cert_serial_hex: str
    revoked_at: str


class RevokedListResponse(BaseModel):
    serials: list[str]
    count: int


class PairResponse(BaseModel):
    device_id: str
    jwt: str
    jwt_ttl_seconds: int
    client_cert_pem_b64: str
    client_key_pem_b64: str
    ca_cert_pem_b64: str
    cert_serial_number_hex: str
    cert_not_after: str
    common_name: str


# ── Dependencies ─────────────────────────────────────────────────────────────


def _redis_dep(request: Request) -> Redis:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        # Fall back to a fresh client; the pairing store is used briefly so a
        # short-lived connection per request is acceptable in dev.
        from redis.asyncio import Redis as _Redis

        redis = _Redis.from_url(get_settings().redis_url)
        request.app.state.redis = redis
    return redis  # type: ignore[no-any-return]


def _pairing_store(redis: Annotated[Redis, Depends(_redis_dep)]) -> PairingCodeStore:
    return PairingCodeStore(redis)


def _revocation_store(redis: Annotated[Redis, Depends(_redis_dep)]) -> RevocationStore:
    return RevocationStore(redis)


def _pairing_rate_limiter(
    redis: Annotated[Redis, Depends(_redis_dep)],
) -> FixedWindowRateLimiter:
    settings = get_settings()
    return FixedWindowRateLimiter(
        redis,
        limit=settings.pairing_rate_limit_per_window,
        window_seconds=settings.pairing_rate_limit_window_seconds,
        namespace="pair",
    )


def _client_ip(request: Request) -> str:
    """
    Best-effort client IP for rate-limit keying. Honours `X-Forwarded-For`
    when the master sits behind a trusted reverse proxy; falls back to the
    raw socket peer.
    """
    fwd = request.headers.get("x-forwarded-for", "").strip()
    if fwd:
        # First entry is the original client; rest are proxies.
        return fwd.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    """
    Minimal operator gate: shared bearer matching `settings.app_secret_key`
    is accepted. Production should replace this with SSO / operator login.
    """
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin bearer token required",
        )
    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, settings.app_secret_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid admin bearer token",
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post(
    "/devices/admin/code",
    response_model=MintCodeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
    summary="Mint a 6-digit pairing code (operator-only).",
)
async def mint_pairing_code(
    body: MintCodeRequest,
    store: Annotated[PairingCodeStore, Depends(_pairing_store)],
) -> MintCodeResponse:
    code = await store.mint(issued_for=body.issued_for)
    return MintCodeResponse(
        code=code.code,
        ttl_seconds=code.ttl_seconds,
        issued_for=code.issued_for or None,
    )


@router.post(
    "/devices/pair",
    response_model=PairResponse,
    summary="Exchange a 6-digit code for a JWT + freshly-minted client cert.",
)
async def pair_device(
    request: Request,
    body: PairRequest,
    store: Annotated[PairingCodeStore, Depends(_pairing_store)],
    limiter: Annotated[FixedWindowRateLimiter, Depends(_pairing_rate_limiter)],
) -> PairResponse:
    decision = await limiter.check(_client_ip(request))
    if not decision.allowed:
        log.warning("device_pairing.rate_limited", ip=_client_ip(request))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many pairing attempts; try again later",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    issued_for = await store.verify(body.code)
    if issued_for is None:
        log.info("device_pairing.code.invalid", device_id=body.device_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="pairing code is invalid, expired, or already used",
        )

    settings = get_settings()
    try:
        creds = mint_client_credentials(body.device_id)
    except FileNotFoundError as exc:
        log.error("device_pairing.cert_mint.missing_ca", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pairing CA is not configured on the master",
        ) from exc

    jwt_token = create_access_token(body.device_id)

    log.info(
        "device_pairing.success",
        device_id=body.device_id,
        issued_for_hint=issued_for or None,
        cert_serial=hex(creds.serial_number),
    )

    return PairResponse(
        device_id=body.device_id,
        jwt=jwt_token,
        jwt_ttl_seconds=settings.jwt_access_token_ttl_seconds,
        client_cert_pem_b64=base64.b64encode(creds.client_cert_pem).decode(),
        client_key_pem_b64=base64.b64encode(creds.client_key_pem).decode(),
        ca_cert_pem_b64=base64.b64encode(creds.ca_cert_pem).decode(),
        cert_serial_number_hex=hex(creds.serial_number),
        cert_not_after=creds.not_after.isoformat(),
        common_name=creds.common_name,
    )


# ── Revocation (operator-only) ───────────────────────────────────────────────


def _normalize_serial(raw: str) -> int:
    s = raw.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s or any(c not in "0123456789abcdef" for c in s):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cert_serial_hex must be a hex string",
        )
    return int(s, 16)


@router.post(
    "/devices/admin/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(_require_admin)],
    summary="Revoke a per-device client cert by its serial (operator-only).",
)
async def revoke_cert(
    body: RevokeRequest,
    revocation: Annotated[RevocationStore, Depends(_revocation_store)],
) -> RevokeResponse:
    import datetime as _dt

    serial_int = _normalize_serial(body.cert_serial_hex)
    await revocation.revoke_cert_serial(serial_int)
    if body.device_id:
        await revocation.revoke_device(body.device_id)
    return RevokeResponse(
        cert_serial_hex=f"{serial_int:x}",
        revoked_at=_dt.datetime.now(_dt.UTC).isoformat(),
    )


@router.get(
    "/devices/admin/revoked",
    response_model=RevokedListResponse,
    dependencies=[Depends(_require_admin)],
    summary="List currently-revoked cert serial numbers (operator-only).",
)
async def list_revoked(
    revocation: Annotated[RevocationStore, Depends(_revocation_store)],
) -> RevokedListResponse:
    serials = await revocation.list_revoked_cert_serials()
    return RevokedListResponse(serials=serials, count=len(serials))


# Suppress an "unused import" warning when JSONResponse isn't referenced
# directly (FastAPI returns models above).
_ = JSONResponse
