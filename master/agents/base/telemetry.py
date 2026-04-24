"""
master.agents.base.telemetry
=============================
TelemetryEmitter: thin wrapper around OTel SDK for agent-level events.
Every agent uses this to emit structured telemetry without touching OTel directly.
Counters, histograms, and spans are all emitted through this single interface.
"""
from __future__ import annotations

import time
from typing import Any

from opentelemetry.metrics import Counter, Histogram

from master.core.telemetry import get_meter, get_tracer

_tracer = get_tracer("master.agents")
_meter = get_meter("master.agents")


class TelemetryEmitter:
    """
    Emits agent-level OpenTelemetry events.
    One shared instance per application process; injected into every agent.
    """

    def __init__(self) -> None:
        self._request_counter: Counter = _meter.create_counter(
            "agent.requests.total",
            description="Total agent task executions",
            unit="1",
        )
        self._error_counter: Counter = _meter.create_counter(
            "agent.errors.total",
            description="Total agent task failures",
            unit="1",
        )
        self._latency_histogram: Histogram = _meter.create_histogram(
            "agent.latency.ms",
            description="Agent task execution latency in milliseconds",
            unit="ms",
        )
        self._cost_histogram: Histogram = _meter.create_histogram(
            "agent.cost.usd",
            description="LLM cost per agent task in USD",
            unit="USD",
        )
        self._token_counter: Counter = _meter.create_counter(
            "agent.tokens.total",
            description="Total tokens consumed by agents",
            unit="1",
        )

    def emit_event(
        self,
        event_type: str,
        agent_id: str,
        task_id: str,
        trace_id: str,
        metadata: dict[str, str] | None = None,
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        error: bool = False,
    ) -> None:
        """
        Emit a structured agent telemetry event.
        Records to OTel counters/histograms and adds an event to the active span.
        Never raises — telemetry failures must not crash agent execution.
        """
        attrs = {
            "agent.id": agent_id,
            "agent.event_type": event_type,
            "task.id": task_id,
            "trace.id": trace_id,
        }
        if metadata:
            for k, v in metadata.items():
                attrs[f"meta.{k}"] = v

        try:
            self._request_counter.add(1, attrs)
            if error:
                self._error_counter.add(1, attrs)
            if latency_ms > 0:
                self._latency_histogram.record(latency_ms, attrs)
            if cost_usd > 0:
                self._cost_histogram.record(cost_usd, attrs)
            if input_tokens > 0 or output_tokens > 0:
                self._token_counter.add(
                    input_tokens + output_tokens,
                    {**attrs, "token.type": "total"},
                )

            # Add span event for trace correlation
            span = _tracer.start_as_current_span(f"agent.event.{event_type}")
            span.__enter__()
            span.__exit__(None, None, None)

        except Exception:
            pass  # Telemetry must not crash agent execution

    def timed_context(self, event_type: str, agent_id: str, task_id: str, trace_id: str) -> "TimedEvent":
        """Context manager that auto-records latency on exit."""
        return TimedEvent(self, event_type, agent_id, task_id, trace_id)


class TimedEvent:
    """Context manager for auto-recording latency on exit."""

    def __init__(
        self,
        emitter: TelemetryEmitter,
        event_type: str,
        agent_id: str,
        task_id: str,
        trace_id: str,
    ) -> None:
        self._emitter = emitter
        self._event_type = event_type
        self._agent_id = agent_id
        self._task_id = task_id
        self._trace_id = trace_id
        self._start: float = 0.0

    def __enter__(self) -> "TimedEvent":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        latency_ms = (time.monotonic() - self._start) * 1000
        self._emitter.emit_event(
            event_type=self._event_type,
            agent_id=self._agent_id,
            task_id=self._task_id,
            trace_id=self._trace_id,
            latency_ms=latency_ms,
            error=exc_type is not None,
        )
