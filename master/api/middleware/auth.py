"""
master.api.middleware.auth
==========================
JWT authentication middleware.
Validates Bearer tokens on every protected request, checks device revocation,
and injects device_id into request.state for downstream use.
Routes listed in EXEMPT_PATHS bypass auth (health, registration).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from master.core.auth.jwt import TokenClaims
from master.core.auth.revocation import RevocationStore
from master.core.exceptions import (
    AuthError,
    DeviceRevokedError,
    InvalidTokenError,
    TokenExpiredError,
)
from master.core.logging import get_logger

log = get_logger(__name__)

# Paths that do not require a valid JWT
EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/",
        "/health/ready",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/auth/device/register",
        "/auth/token/refresh",
        "/devices/pair",
        "/devices/admin/code",
        "/devices/admin/revoke",
        "/devices/admin/revoked",
        "/admin/devices",
        "/admin/login",
        "/admin/whoami",
    }
)


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Validates Bearer JWT on every non-exempt request.
    On success: injects request.state.device_id and request.state.jti.
    On failure: returns 401 JSON response immediately.
    """

    def __init__(self, app: object, revocation_store: RevocationStore) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._revocation = revocation_store

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "missing_token", "message": "Authorization header required"},
            )

        token = auth_header.removeprefix("Bearer ").strip()

        try:
            from master.core.auth.jwt import validate_access_token

            payload = validate_access_token(token)
            device_id: str = payload[TokenClaims.SUBJECT]
            jti: str = payload.get(TokenClaims.JTI, "")

            # Check device-level revocation
            if await self._revocation.is_device_revoked(device_id):
                raise DeviceRevokedError(f"Device '{device_id}' has been revoked")

            # Check per-token revocation (logout / JTI block)
            if jti and await self._revocation.is_jti_revoked(jti):
                raise InvalidTokenError("Token has been revoked")

        except TokenExpiredError as exc:
            log.info("auth.token.expired", path=request.url.path)
            return JSONResponse(
                status_code=401,
                content={"error": "token_expired", "message": str(exc)},
            )
        except AuthError as exc:
            log.warning("auth.failed", path=request.url.path, error=str(exc))
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": str(exc)},
            )

        request.state.device_id = device_id
        request.state.jti = jti
        return await call_next(request)
