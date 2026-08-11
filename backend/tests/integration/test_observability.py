"""OpenTelemetry bootstrap.

These tests assert that tracing is opt-in, that enabling it does not require a
collector, and that shutdown is safe. They do not assert on span content: that
belongs to Phase 13, where the exporter pipeline is actually wired up.
"""

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core.observability import (
    configure_tracing,
    current_trace_ids,
    instrument_fastapi,
    shutdown_tracing,
)
from app.core.settings import ObservabilitySettings
from tests.conftest import build_settings


def test_tracing_is_disabled_by_default() -> None:
    settings = build_settings()

    assert settings.observability.tracing_enabled is False
    assert configure_tracing(settings) is None


def test_no_trace_identifiers_outside_a_span() -> None:
    assert current_trace_ids() == (None, None)


def test_a_disabled_provider_shuts_down_cleanly() -> None:
    shutdown_tracing(None)


def test_the_console_exporter_needs_no_external_backend() -> None:
    settings = build_settings(
        observability=ObservabilitySettings(tracing_enabled=True, exporter="console"),
    )
    provider = configure_tracing(settings)
    try:
        assert provider is not None
    finally:
        shutdown_tracing(provider)


def test_instrumentation_is_a_no_op_when_tracing_is_disabled() -> None:
    settings = build_settings()
    app = create_app(settings)

    instrument_fastapi(app, settings)

    with TestClient(app) as test_client:
        assert test_client.get("/health/live").status_code == 200


def test_the_application_serves_requests_with_tracing_enabled() -> None:
    settings = build_settings(
        observability=ObservabilitySettings(tracing_enabled=True, exporter="console"),
    )
    app = create_app(settings)
    try:
        with TestClient(app) as test_client:
            response = test_client.get("/health/live")

        assert response.status_code == 200
        assert response.headers["X-Request-ID"]
    finally:
        shutdown_tracing(app.state.tracer_provider)


def test_trace_identifiers_reach_the_logging_context_when_sampled() -> None:
    settings = build_settings(
        observability=ObservabilitySettings(tracing_enabled=True, exporter="console"),
    )
    provider = configure_tracing(settings)
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("tests")
        with tracer.start_as_current_span("unit-test-span"):
            trace_id, span_id = current_trace_ids()

        assert trace_id is not None
        assert span_id is not None
        assert len(trace_id) == 32
        assert len(span_id) == 16
    finally:
        shutdown_tracing(provider)
