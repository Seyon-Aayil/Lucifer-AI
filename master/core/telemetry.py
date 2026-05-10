"""
master.core.telemetry
=====================
OpenTelemetry SDK setup: traces, metrics, logs.
Called once via setup_telemetry() at app startup.
Provides get_tracer() for creating trace spans in application code.
"""

from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from master.core.config import get_settings


def setup_telemetry() -> None:
    """
    Configure OpenTelemetry SDK.
    - Traces: exported via OTLP gRPC to OTel Collector → Jaeger
    - Metrics: exported via OTLP gRPC to OTel Collector → Prometheus
    Call exactly once at application startup.
    """
    settings = get_settings()

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.app_env.value,
        }
    )

    # ── Trace Provider ────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Metric Provider ───────────────────────────────────────────────────
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True),
        export_interval_millis=15_000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)


def get_tracer(name: str) -> trace.Tracer:
    """Return a named tracer for creating spans in application code."""
    return trace.get_tracer(name, schema_url="https://opentelemetry.io/schemas/1.25.0")


def get_meter(name: str) -> metrics.Meter:
    """Return a named meter for creating instruments in application code."""
    return metrics.get_meter(name)
