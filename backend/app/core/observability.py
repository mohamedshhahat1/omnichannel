"""OpenTelemetry bootstrap (ADR-0010).

Phase 1 establishes the foundation only:

- a tracer provider with correct service resource attributes,
- a configurable exporter (`none`, `console`, `otlp`),
- W3C trace context and baggage as the global propagators,
- FastAPI instrumentation,
- clean shutdown that flushes buffered spans.

Tracing is disabled by default, so local development and CI need no tracing
backend. Even when disabled, `current_trace_ids` degrades quietly to `None`,
so the logging layer never has to care whether tracing is on.

The propagator configuration matters more than it looks: it is the contract
that later phases rely on to carry `traceparent` from an HTTP request into a
`webhook_events` row, into an `outbox_events` row, into a Celery header, and
finally into an AI run and a provider request (`docs/observability.md`).

Metrics (Prometheus) and error tracking (Sentry) are Phase 13 and are
deliberately not started here.

OpenTelemetry imports are function-local. They are real dependencies, but
keeping them out of module scope means the logging layer and the tooling that
imports it stay usable even if the SDK is absent or fails to initialise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider

    from app.core.settings import Settings

logger = logging.getLogger(__name__)


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return the active `(trace_id, span_id)` as hex, or `(None, None)`.

    Never raises. Called on every log record, so it must be cheap and total.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None, None

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None

    trace_id: int = span_context.trace_id
    span_id: int = span_context.span_id
    return format(trace_id, "032x"), format(span_id, "016x")


def configure_tracing(settings: Settings) -> TracerProvider | None:
    """Initialise tracing. Returns the provider, or None when disabled."""
    if not settings.observability.tracing_enabled:
        logger.info("observability.tracing.disabled")
        return None

    from opentelemetry import trace
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment.value,
        }
    )

    # Parent-based so that a sampling decision made upstream is honoured for the
    # whole workflow; ratio-based only for traces that start here.
    sampler = ParentBased(root=TraceIdRatioBased(settings.observability.sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)

    exporter = settings.observability.exporter
    if exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.observability.otlp_endpoint))
        )

    trace.set_tracer_provider(provider)
    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    logger.info(
        "observability.tracing.enabled",
        extra={
            "exporter": exporter,
            "sample_ratio": settings.observability.sample_ratio,
        },
    )
    return provider


def instrument_fastapi(app: FastAPI, settings: Settings) -> None:
    """Attach FastAPI auto-instrumentation when tracing is enabled."""
    if not settings.observability.tracing_enabled:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=settings.observability.excluded_urls or None,
    )
    logger.info("observability.instrumentation.fastapi.enabled")


def shutdown_tracing(provider: TracerProvider | None) -> None:
    """Flush and shut down the tracer provider.

    Called during application shutdown so buffered spans are exported instead
    of discarded. Failure here must never prevent the process from exiting.
    """
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:
        logger.exception("observability.tracing.shutdown_failed")
    else:
        logger.info("observability.tracing.shutdown")
