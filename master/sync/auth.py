"""
master.sync.auth
=================
gRPC server interceptor that enforces device authentication.

Each incoming RPC must include `authorization: Bearer <jwt>` metadata.
The interceptor:
  1. Extracts and validates the JWT (master.core.auth.jwt).
  2. Checks Redis revocation (RevocationStore) for the device and JTI.
  3. Stores `device_id` and `jti` on a contextvar accessible from the servicer.
  4. Rejects with `UNAUTHENTICATED` on any failure.

mTLS is handled at the channel level (server credentials in runtime.py).
The peer certificate's CN is treated as advisory; the JWT subject is
the authoritative device identity.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

import grpc

from master.core.auth.jwt import TokenClaims, validate_access_token
from master.core.auth.revocation import RevocationStore
from master.core.exceptions import InvalidTokenError, TokenExpiredError
from master.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CallerIdentity:
    device_id: str
    jti: str


# Set per-RPC by the interceptor; servicer methods read from here.
current_caller: ContextVar[CallerIdentity | None] = ContextVar("lucifer_sync_caller", default=None)


class DeviceAuthInterceptor(grpc.aio.ServerInterceptor):
    """
    Async gRPC interceptor: validates JWT + revocation.
    Rejects RPCs lacking valid `authorization` metadata.
    """

    _UNAUTH = grpc.StatusCode.UNAUTHENTICATED

    def __init__(self, revocation_store: RevocationStore) -> None:
        self._revocation = revocation_store

    async def intercept_service(  # type: ignore[override]
        self,
        continuation,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        metadata = dict(handler_call_details.invocation_metadata or [])
        auth_header = metadata.get("authorization", "")

        if not auth_header.lower().startswith("bearer "):
            return self._reject("missing or malformed authorization metadata")

        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = validate_access_token(token)
        except TokenExpiredError:
            return self._reject("access token expired")
        except InvalidTokenError as exc:
            return self._reject(f"invalid token: {exc}")

        device_id = payload.get(TokenClaims.SUBJECT)
        jti = payload.get(TokenClaims.JTI)
        if not device_id or not jti:
            return self._reject("token missing subject or jti")

        if await self._revocation.is_device_revoked(device_id):
            return self._reject(f"device {device_id} is revoked")
        if await self._revocation.is_jti_revoked(jti):
            return self._reject(f"jti {jti} is revoked")

        # Wrap continuation to set the contextvar inside the RPC's coroutine.
        handler = await continuation(handler_call_details)
        if handler is None:
            return None
        return _wrap_handler(handler, CallerIdentity(device_id=device_id, jti=jti))

    @classmethod
    def _reject(cls, message: str) -> grpc.RpcMethodHandler:
        log.warning("sync.auth.rejected", reason=message)

        async def deny_unary_unary(_request, context):
            await context.abort(cls._UNAUTH, message)

        async def deny_unary_stream(_request, context):
            await context.abort(cls._UNAUTH, message)

        async def deny_stream_unary(_iterator, context):
            await context.abort(cls._UNAUTH, message)

        async def deny_stream_stream(_iterator, context):
            await context.abort(cls._UNAUTH, message)

        # Returning a unary_unary handler is sufficient — gRPC will use it
        # regardless of method type because the abort short-circuits.
        return grpc.unary_unary_rpc_method_handler(deny_unary_unary)


def _wrap_handler(
    handler: grpc.RpcMethodHandler,
    identity: CallerIdentity,
) -> grpc.RpcMethodHandler:
    """Wrap each behaviour to set the caller contextvar before invocation."""

    def wrap(behavior):
        if behavior is None:
            return None

        async def unary(request, context):
            current_caller.set(identity)
            return await behavior(request, context)

        async def stream(request_iterator, context):
            current_caller.set(identity)
            async for response in behavior(request_iterator, context):
                yield response

        return unary if not handler.request_streaming and not handler.response_streaming else stream

    if handler.request_streaming and handler.response_streaming:
        return grpc.stream_stream_rpc_method_handler(
            wrap(handler.stream_stream),
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.request_streaming:

        async def stream_unary(request_iterator, context):
            current_caller.set(identity)
            return await handler.stream_unary(request_iterator, context)

        return grpc.stream_unary_rpc_method_handler(
            stream_unary,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.response_streaming:

        async def unary_stream(request, context):
            current_caller.set(identity)
            async for response in handler.unary_stream(request, context):
                yield response

        return grpc.unary_stream_rpc_method_handler(
            unary_stream,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    async def unary_unary(request, context):
        current_caller.set(identity)
        return await handler.unary_unary(request, context)

    return grpc.unary_unary_rpc_method_handler(
        unary_unary,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )
