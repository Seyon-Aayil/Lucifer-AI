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

from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

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


class DeviceAuthInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]  # grpc.aio is untyped
    """
    Async gRPC interceptor: validates JWT + revocation.
    Rejects RPCs lacking valid `authorization` metadata.
    """

    _UNAUTH = grpc.StatusCode.UNAUTHENTICATED

    def __init__(self, revocation_store: RevocationStore) -> None:
        self._revocation = revocation_store

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
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
        return _wrap_handler(
            handler,
            CallerIdentity(device_id=device_id, jti=jti),
            self._revocation,
        )

    @classmethod
    def _reject(cls, message: str) -> grpc.RpcMethodHandler:
        log.warning("sync.auth.rejected", reason=message)

        async def deny_unary_unary(_request: Any, context: grpc.aio.ServicerContext) -> None:
            await context.abort(cls._UNAUTH, message)

        # Returning a unary_unary handler is sufficient — gRPC will use it
        # regardless of method type because the abort short-circuits.
        return grpc.unary_unary_rpc_method_handler(deny_unary_unary)


def _wrap_handler(
    handler: grpc.RpcMethodHandler,
    identity: CallerIdentity,
    revocation_store: RevocationStore,
) -> grpc.RpcMethodHandler:
    """
    Wrap each behaviour to:
      1. Set the caller contextvar so the servicer can read `current_caller`.
      2. Verify the peer mTLS cert serial isn't on the revocation set —
         per-cert kill-switch independent of JWT lifetime.
    """

    async def _verify_peer_cert(context: grpc.aio.ServicerContext) -> bool:
        serial = _peer_cert_serial(context)
        if serial is None:
            # No peer cert exposed (insecure dev channel) — JWT path is the
            # only auth gate; allow.
            return True
        if await revocation_store.is_cert_serial_revoked(serial):
            log.warning(
                "sync.auth.cert_revoked",
                serial=hex(serial),
                device_id=identity.device_id,
            )
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"client cert serial {hex(serial)} is revoked",
            )
            return False
        return True

    if handler.request_streaming and handler.response_streaming:

        async def stream_stream(
            request_iterator: AsyncIterator[Any], context: grpc.aio.ServicerContext
        ) -> AsyncIterator[Any]:
            if not await _verify_peer_cert(context):
                return
            current_caller.set(identity)
            async for response in handler.stream_stream(request_iterator, context):
                yield response

        return grpc.stream_stream_rpc_method_handler(
            stream_stream,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.request_streaming:

        async def stream_unary(
            request_iterator: AsyncIterator[Any], context: grpc.aio.ServicerContext
        ) -> Any:
            if not await _verify_peer_cert(context):
                return None
            current_caller.set(identity)
            return await handler.stream_unary(request_iterator, context)

        return grpc.stream_unary_rpc_method_handler(
            stream_unary,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
    if handler.response_streaming:

        async def unary_stream(
            request: Any, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[Any]:
            if not await _verify_peer_cert(context):
                return
            current_caller.set(identity)
            async for response in handler.unary_stream(request, context):
                yield response

        return grpc.unary_stream_rpc_method_handler(
            unary_stream,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )

    async def unary_unary(request: Any, context: grpc.aio.ServicerContext) -> Any:
        if not await _verify_peer_cert(context):
            return None
        current_caller.set(identity)
        return await handler.unary_unary(request, context)

    return grpc.unary_unary_rpc_method_handler(
        unary_unary,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )


def _peer_cert_serial(context: grpc.aio.ServicerContext) -> int | None:
    """
    Extract the peer mTLS certificate's serial number from a gRPC servicer
    context. Returns `None` if no peer cert is present (insecure channel
    used in dev) or the cert can't be parsed.
    """
    try:
        auth = context.auth_context() if hasattr(context, "auth_context") else {}
    except Exception:  # noqa: BLE001 — gRPC raises a wide set of types
        return None
    pem_chain = auth.get("x509_pem_cert") if auth else None
    if not pem_chain:
        return None
    pem = pem_chain[0] if isinstance(pem_chain, list) else pem_chain
    pem_bytes = pem if isinstance(pem, bytes) else str(pem).encode()
    try:
        from cryptography import x509  # local import to keep grpc-only callers happy

        cert = x509.load_pem_x509_certificate(pem_bytes)
        return int(cert.serial_number)
    except Exception:  # noqa: BLE001
        return None
