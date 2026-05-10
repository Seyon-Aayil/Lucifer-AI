"""
master.core.auth.operator
==========================
Operator login + session management for the master web admin.

Single-operator self-hosted model:
- Username + argon2id-hashed password supplied via ENV
  (`LUCIFER_OPERATOR_USERNAME`, `LUCIFER_OPERATOR_PASSWORD_HASH`).
- A successful `/admin/login` mints an *operator* JWT distinct from device
  JWTs (claim `type=operator_session`).
- `verify_operator_session` is the dependency used by `/devices/admin/*`
  to gate operator-only endpoints.

Generate the password hash with:

    python -c "from argon2 import PasswordHasher; \
        print(PasswordHasher().hash('your-password'))"
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt

from master.core.auth.jwt import TokenClaims
from master.core.config import get_settings
from master.core.exceptions import InvalidTokenError, TokenExpiredError
from master.core.logging import get_logger

log = get_logger(__name__)

_OPERATOR_TYPE = "operator_session"
_PH = PasswordHasher()


class InvalidCredentialsError(Exception):
    """Raised when the operator login fails."""


def is_operator_login_configured() -> bool:
    """True iff env-supplied operator credentials are present."""
    s = get_settings()
    return bool(s.operator_username and s.operator_password_hash)


def verify_password(password: str) -> bool:
    """
    Constant-time-ish password verification against the configured argon2
    hash. Returns False on mismatch (or when login is not configured).
    """
    if not is_operator_login_configured():
        return False
    s = get_settings()
    try:
        return _PH.verify(s.operator_password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception as exc:  # invalid hash format etc.
        log.error("operator.verify.error", error=str(exc))
        return False


def login(username: str, password: str) -> str:
    """
    Validate username + password, return a freshly-minted operator session
    JWT. Raises `InvalidCredentialsError` on any auth failure (timing-safe
    string compare on the username).
    """
    if not is_operator_login_configured():
        raise InvalidCredentialsError("operator login is not configured")
    s = get_settings()
    if not secrets.compare_digest(username, s.operator_username):
        raise InvalidCredentialsError("invalid credentials")
    if not verify_password(password):
        raise InvalidCredentialsError("invalid credentials")
    return _create_operator_session_token(username)


def _create_operator_session_token(username: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        TokenClaims.SUBJECT: username,
        TokenClaims.TOKEN_TYPE: _OPERATOR_TYPE,
        TokenClaims.JTI: secrets.token_hex(16),
        TokenClaims.ISSUED_AT: now,
        TokenClaims.EXPIRY: now + timedelta(seconds=s.operator_session_ttl_seconds),
    }
    return jwt.encode(payload, s.app_secret_key, algorithm=s.jwt_algorithm)  # type: ignore[no-any-return]


def validate_operator_session(token: str) -> dict[str, Any]:
    """
    Decode + validate an operator session JWT. Raises `TokenExpiredError`
    or `InvalidTokenError` on any failure.
    """
    s = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            s.app_secret_key,
            algorithms=[s.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("operator session expired") from exc
    except JWTError as exc:
        raise InvalidTokenError(f"invalid operator session: {exc}") from exc

    if payload.get(TokenClaims.TOKEN_TYPE) != _OPERATOR_TYPE:
        raise InvalidTokenError("token is not an operator session")
    return payload
