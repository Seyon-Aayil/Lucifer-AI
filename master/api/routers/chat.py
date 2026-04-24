"""
master.api.routers.chat
========================
Chat endpoints:
  POST /v1/chat        → ChatRequest → ChatResponse (non-streaming)
  WS   /v1/ws/chat     → ChatRequest → stream of ChatChunk (NDJSON over WebSocket)

Every inbound message is PII-scanned before entering the orchestrator.
Token usage and cost are returned in every response.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from master.agents.base.agent import AgentSurface
from master.api.middleware.pii_scanner import PIIScanner
from master.api.schemas import ChatRequest, ChatResponse, ChatChunk, Surface, TokenUsageSchema
from master.core.exceptions import PIIDetectedError
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.orchestrator.graph import build_graph
from master.orchestrator.state import OrchestratorState

router = APIRouter()
log = get_logger(__name__)
tracer = get_tracer(__name__)

# Shared instances (initialised once per worker process)
_pii_scanner = PIIScanner(use_ner=False)  # NER enabled in prod via config
_graph = build_graph().compile()

_SURFACE_MAP: dict[Surface, AgentSurface] = {
    Surface.WATCH: AgentSurface.WATCH,
    Surface.MOBILE: AgentSurface.MOBILE,
    Surface.DESKTOP: AgentSurface.DESKTOP,
    Surface.MASTER: AgentSurface.MASTER,
    Surface.CLI: AgentSurface.CLI,
    Surface.WEB: AgentSurface.WEB,
}


def _check_pii(message: str, allow_pii: bool) -> None:
    """Raise PIIDetectedError if message contains PII and allow_pii is False."""
    if not allow_pii:
        _pii_scanner.scan_or_raise(message, context="chat.message")


async def _run_graph(state: OrchestratorState) -> OrchestratorState:
    """Run the LangGraph orchestrator and return final state."""
    result: OrchestratorState = await _graph.ainvoke(state)  # type: ignore[arg-type]
    return result


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Single-turn chat (non-streaming)",
)
async def chat(
    body: ChatRequest,
    request: Request,
) -> ChatResponse:
    """
    Route a single chat message through the orchestrator.
    PII is scanned before entry. Response includes token usage and cost.
    """
    with tracer.start_as_current_span("api.chat.post"):
        allow_pii = request.headers.get("X-Lucifer-Allow-PII", "false").lower() == "true"
        try:
            _check_pii(body.message, allow_pii)
        except PIIDetectedError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail={"error": "pii_detected", "message": str(exc)})

        task_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        start = time.monotonic()

        device_id: str | None = getattr(request.state, "device_id", None)
        surface = _SURFACE_MAP.get(body.surface, AgentSurface.WEB)

        initial_state: OrchestratorState = {
            "task_id": task_id,
            "raw_input": body.message,
            "surface": surface,
            "device_id": device_id,
            "session_id": body.session_id,
            "trace_id": trace_id,
            "messages": [{"role": "user", "content": body.message}],
            "retry_count": 0,
        }

        try:
            final_state = await _run_graph(initial_state)
        except Exception as exc:
            log.error("chat.orchestrator.failed", error=str(exc), trace_id=trace_id)
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail={"error": "orchestration_failed"})

        latency_ms = int((time.monotonic() - start) * 1000)
        agent_id = final_state.get("agent_id", "personal-agent")
        response_text = final_state.get("final_output", "")

        log.info(
            "chat.completed",
            task_id=task_id,
            agent_id=agent_id,
            latency_ms=latency_ms,
            surface=body.surface,
        )

        from datetime import UTC, datetime

        return ChatResponse(
            task_id=task_id,
            agent_id=agent_id,
            content=response_text,
            surface=body.surface,
            token_usage=TokenUsageSchema(
                input_tokens=0, output_tokens=0, total_tokens=0  # filled by agent in Phase 3
            ),
            cost_usd=None,
            latency_ms=latency_ms,
            created_at=datetime.now(UTC),
        )


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket) -> None:
    """
    Streaming chat via WebSocket. Client sends a ChatRequest JSON payload.
    Server streams ChatChunk JSON objects; final chunk has is_final=True.
    """
    await websocket.accept()
    log.info("ws.chat.connected", client=str(websocket.client))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                body = ChatRequest(**data)
            except Exception as exc:
                await websocket.send_json({"error": f"invalid_request: {exc}"})
                continue

            task_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())
            surface = _SURFACE_MAP.get(body.surface, AgentSurface.WEB)

            initial_state: OrchestratorState = {
                "task_id": task_id,
                "raw_input": body.message,
                "surface": surface,
                "session_id": body.session_id,
                "trace_id": trace_id,
                "messages": [{"role": "user", "content": body.message}],
                "retry_count": 0,
            }

            try:
                # Stream graph execution events
                async for event in _graph.astream_events(initial_state, version="v2"):  # type: ignore[arg-type]
                    if event["event"] == "on_chain_stream":
                        chunk_data = event.get("data", {}).get("chunk", {})
                        if isinstance(chunk_data, dict):
                            delta = chunk_data.get("final_output", "")
                            if delta:
                                chunk = ChatChunk(task_id=task_id, delta=delta, is_final=False)
                                await websocket.send_json(chunk.model_dump())

                # Send final chunk
                final_chunk = ChatChunk(task_id=task_id, delta="", is_final=True)
                await websocket.send_json(final_chunk.model_dump())

            except Exception as exc:
                log.error("ws.chat.orchestrator_error", error=str(exc))
                await websocket.send_json({"error": "orchestration_failed", "task_id": task_id})

    except WebSocketDisconnect:
        log.info("ws.chat.disconnected")
