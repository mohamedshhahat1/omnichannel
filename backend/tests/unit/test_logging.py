"""Structured logging: field shape, correlation context and redaction."""

import json
import logging
import sys
from typing import Any

from app.core.logging import (
    REDACTED,
    TRUNCATED,
    ConsoleFormatter,
    ContextFilter,
    JsonFormatter,
    is_sensitive_key,
    redact,
)
from app.platform.correlation import CorrelationIds, bind, unbind


def make_record(**context: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event.happened",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(context)
    return record


def apply_context(record: logging.LogRecord) -> logging.LogRecord:
    context_filter = ContextFilter(
        service="omnichannel-api",
        environment="test",
        version="0.1.0",
    )
    context_filter.filter(record)
    return record


def format_json(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return payload


def test_json_output_carries_process_identity_and_timestamp() -> None:
    payload = format_json(apply_context(make_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "event.happened"
    assert payload["service"] == "omnichannel-api"
    assert payload["environment"] == "test"
    assert payload["version"] == "0.1.0"
    assert payload["timestamp"].endswith("+00:00")


def test_output_is_a_single_line() -> None:
    line = JsonFormatter().format(apply_context(make_record()))
    assert "\n" not in line


def test_correlation_identifiers_are_taken_from_the_ambient_context() -> None:
    tokens = bind(CorrelationIds(request_id="request-1234", correlation_id="workflow-1234"))
    try:
        payload = format_json(apply_context(make_record()))
    finally:
        unbind(tokens)

    assert payload["request_id"] == "request-1234"
    assert payload["correlation_id"] == "workflow-1234"


def test_correlation_fields_are_absent_outside_a_request() -> None:
    payload = format_json(apply_context(make_record()))

    assert "request_id" not in payload
    assert "trace_id" not in payload


def test_caller_context_is_preserved() -> None:
    payload = format_json(apply_context(make_record(provider="whatsapp", attempt=2)))

    assert payload["context"] == {"provider": "whatsapp", "attempt": 2}


def test_credential_shaped_keys_are_redacted() -> None:
    record = make_record(
        password="hunter2",
        api_key="sk-live-0123456789",
        authorization="Bearer abcdef.token.value",
        cookie="oc_session=abc123",
        tenant_id="tenant-42",
    )
    payload = format_json(apply_context(record))
    context = payload["context"]

    for key in ("password", "api_key", "authorization", "cookie"):
        assert context[key] == REDACTED
    assert context["tenant_id"] == "tenant-42"

    serialised = json.dumps(payload)
    for secret in ("hunter2", "sk-live-0123456789", "abcdef.token.value", "oc_session=abc123"):
        assert secret not in serialised


def test_nested_credentials_are_redacted() -> None:
    record = make_record(
        provider_request={
            "url": "https://graph.facebook.com/v21.0/me/messages",
            "headers": {"Authorization": "Bearer provider-secret"},
        }
    )
    payload = format_json(apply_context(record))
    provider_request = payload["context"]["provider_request"]

    assert provider_request["headers"]["Authorization"] == REDACTED
    assert provider_request["url"] == "https://graph.facebook.com/v21.0/me/messages"
    assert "provider-secret" not in json.dumps(payload)


def test_credentials_inside_lists_are_redacted() -> None:
    record = make_record(attempts=[{"api_key": "sk-live-1"}, {"api_key": "sk-live-2"}])
    payload = format_json(apply_context(record))

    assert payload["context"]["attempts"] == [{"api_key": REDACTED}, {"api_key": REDACTED}]


def test_redaction_depth_is_bounded() -> None:
    root: dict[str, Any] = {"depth": 0}
    node = root
    for depth in range(1, 12):
        child: dict[str, Any] = {"depth": depth}
        node["child"] = child
        node = child

    assert TRUNCATED in json.dumps(redact(root))


def test_key_matching_normalises_separators_and_case() -> None:
    assert is_sensitive_key("Set-Cookie")
    assert is_sensitive_key("X-API-Key")
    assert is_sensitive_key("refresh_token")
    assert not is_sensitive_key("tenant_id")
    assert not is_sensitive_key("conversation_id")


def test_exception_detail_is_kept_for_operators() -> None:
    try:
        raise ValueError("internal failure detail")
    except ValueError:
        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="operation.failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = format_json(apply_context(record))

    assert "ValueError" in payload["exception"]
    assert "internal failure detail" in payload["exception"]


def test_console_output_is_readable_and_still_redacts() -> None:
    record = apply_context(make_record(password="hunter2"))
    line = ConsoleFormatter().format(record)

    assert "event.happened" in line
    assert "INFO" in line
    assert REDACTED in line
    assert "hunter2" not in line
