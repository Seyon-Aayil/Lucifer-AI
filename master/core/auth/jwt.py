"""
master.core.auth.jwt
====================
JWT issuance and validation for device authentication.
Access tokens: 1h TTL, signed HS256.
Refresh tokens: 7d TTL, stored as hashed single-use tokens.
All validation raises domain exceptions — never returns None on failure.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt

from master.core.config import get_settings
from master.core.exceptions import InvalidTokenError, TokenExpiredError
from master.core.logging import get_logger

log = get_logger(__name__)


def _settings() -> Any:
    return get_settings()


# ── Token Payload Schema ──────────────────────────────────────────────────────


class TokenClaims:
    """Standard claim keys used in Lucifer JWTs."""

    SUBJECT = "sub"  # device_id
    ISSUED_AT = "iat"
    EXPIRY = "exp"
    TOKEN_TYPE = "type"  # "access" | "refresh"
    JTI = "jti"  # Unique token ID (for revocation)


# ── Access Token ──────────────────────────────────────────────────────────────


def create_access_token(device_id: str) -> str:
    """
    Issue a short-lived access JWT for the given device.
    Returns: signed JWT string.
    """
    settings = _settings()
    now = datetime.now(UTC)
    payload = {
        TokenClaims.SUBJECT: device_id,
        TokenClaims.TOKEN_TYPE: "access",
        TokenClaims.JTI: secrets.token_hex(16),
        TokenClaims.ISSUED_AT: now,
        TokenClaims.EXPIRY: now + timedelta(seconds=settings.jwt_access_token_ttl_seconds),
    }
    token: str = jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)
    log.debug("jwt.access_token.issued", device_id=device_id)
    return token


def validate_access_token(token: str) -> dict[str, Any]:
    """
    Validate an access JWT and return its decoded payload.
    Raises:
        TokenExpiredError: token has passed its expiry.
        InvalidTokenError: signature invalid, malformed, or wrong type.
    """
    settings = _settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.app_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Access token has expired") from exc
    except JWTError as exc:
        raise InvalidTokenError(f"Invalid access token: {exc}") from exc

    if payload.get(TokenClaims.TOKEN_TYPE) != "access":
        raise InvalidTokenError("Token is not an access token")

    return payload


def get_device_id_from_token(token: str) -> str:
    """Validate token and extract the device_id (subject claim)."""
    payload = validate_access_token(token)
    device_id: str | None = payload.get(TokenClaims.SUBJECT)
    if not device_id:
        raise InvalidTokenError("Token missing 'sub' claim")
    return device_id


# ── Refresh Token ─────────────────────────────────────────────────────────────


def create_refresh_token() -> tuple[str, str]:
    """
    Generate a cryptographically random single-use refresh token.
    Returns: (raw_token, sha256_hash_of_token).
    Store only the hash in the database.
    """
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def hash_refresh_token(raw_token: str) -> str:
    """Compute the SHA-256 hash of a raw refresh token for DB lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()
