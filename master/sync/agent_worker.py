"""
master.sync.agent_worker
========================
Consumes the `agent.task.>` JetStream subject that the gRPC servicer publishes
for every offline ``QueuedAction`` an edge device flushes during ``SyncStream``,
dispatches each task through the LangGraph orchestrator, and publishes the
agent's response to ``agent.result.<device_id>`` for the edge to pick up.

This closes the loop that previously dead-ended: actions were enqueued on the
edge, dispatched to NATS by the servicer, but never executed master-side.

Contract for the inner action payload (the edge ``QueuedAction.payload`` bytes,
hex-encoded in the envelope): a JSON object ``{"raw_input": str, "intent"?: str}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from master.agents.base.agent import AgentSurface
from master.core.logging import get_logger

log = get_logger(__name__)

_SUBJECT = "agent.task.>"
_CONSUMER_NAME = "lucifer-agent-task-worker"
_RESULTS_KEY = "lucifer:agent_results:{device_id}"
_RESULTS_CAP = 100


def results_key(device_id: str) -> str:
    return _RESULTS_KEY.format(device_id=device_id)


# A dispatch function turns a normalised request dict into a result dict.
DispatchFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def make_orchestrator_dispatch(graph: Any) -> DispatchFn:
    """
    Build a dispatch function that runs a request through the LangGraph
    orchestrator (the same entry the /chat route uses) and extracts the agent
    response. `graph` is the compiled graph stored on `app.state.graph`.
    """

    async def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id") or str(uuid.uuid4())
        raw_input = request.get("raw_input", "")
        state: dict[str, Any] = {
            "task_id": task_id,
            "raw_input": raw_input,
            "surface": AgentSurface.DESKTOP,
            "device_id": request.get("device_id"),
            "trace_id": str(uuid.uuid4()),
            "messages": [{"role": "user", "content": raw_input}],
            "retry_count": 0,
        }
        result: dict[str, Any] = await graph.ainvoke(
            state, config={"configurable": {"thread_id": task_id}}
        )
        return {
            "task_id": task_id,
            "agent_id": result.get("agent_id", "personal-agent"),
            "final_output": result.get("final_output", ""),
        }

    return _dispatch


class AgentTaskWorker:
    """
    Long-running JetStream consumer. Started by the FastAPI lifespan; one per
    process. Each `agent.task.>` message is parsed, dispatched, the result
    published back, and the message acked (or nak'd on failure for redelivery).
    """

    def __init__(
        self,
        nats_js: Any,
        dispatch: DispatchFn,
        audit_logger: Any = None,
        redis: Any = None,
    ) -> None:
        self._js = nats_js
        self._dispatch = dispatch
        self._audit = audit_logger
        self._redis = redis
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="agent_task_worker")
        log.info("agent_task_worker.started")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("agent_task_worker.stopped")

    async def _run(self) -> None:
        try:
            sub = await self._js.subscribe(_SUBJECT, durable=_CONSUMER_NAME)
            async for msg in sub.messages:
                await self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log and keep the worker alive
            log.error("agent_task_worker.error", error=str(exc))

    async def _handle(self, msg: Any) -> None:
        try:
            request = self._parse(msg.data)
            device_id = request["device_id"]
            result = await self._dispatch(request)
            result.setdefault("completed_at", int(time.time() * 1000))
            payload = json.dumps(result).encode()
            await self._js.publish(f"agent.result.{device_id}", payload)
            # Buffer for the edge to pull over gRPC (it has no NATS access).
            if self._redis is not None:
                key = results_key(device_id)
                await self._redis.lpush(key, payload)
                await self._redis.ltrim(key, 0, _RESULTS_CAP - 1)
            log.info(
                "agent_task_worker.dispatched",
                device_id=device_id,
                task_id=request.get("task_id"),
                agent_id=result.get("agent_id"),
            )
            await msg.ack()
        except Exception as exc:  # noqa: BLE001 — nak for redelivery, never crash
            log.error("agent_task_worker.handle_error", error=str(exc))
            await msg.nak()

    @staticmethod
    def _parse(data: bytes) -> dict[str, Any]:
        """Decode the servicer envelope + the hex-encoded inner action payload."""
        envelope = json.loads(data)
        inner: dict[str, Any] = {}
        payload_hex = envelope.get("payload")
        if payload_hex:
            with contextlib.suppress(ValueError, json.JSONDecodeError):
                inner = json.loads(bytes.fromhex(payload_hex))
        return {
            "task_id": envelope.get("action_id"),
            "device_id": envelope.get("device_id", "unknown"),
            "action_type": envelope.get("action_type"),
            "raw_input": inner.get("raw_input", ""),
            "intent": inner.get("intent"),
        }
