"""Structured application logging.

Production emits one JSON object per line on stdout, which is what a container
log collector expects. Development can use a compact human-readable format.

Every record automatically carries the identity of the process and of the unit
of work: `service`, `environment`, `version`, `request_id`, `correlation_id`
and - when tracing is on - `trace_id` and `span_id`. Call sites never pass
these by hand.

Redaction (ENGINEERING.md 5.1, 5.10): structured context passed via `extra` is
walked and any key whose name looks like a credential is replaced before it is
serialised. This is a safety net, not a licence - the rule is still to not log
sensitive values in the first place. It cannot inspect values interpolated into
a message string, so never format a secret into a log message.

Usage::

    logger.info("webhook.received", extra={"provider": "whatsapp"})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.observability import current_trace_ids
from app.platform.correlation import get_correlation_id, get_request_id

if TYPE_CHECKING:
    from app.core.settings import Settings

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_MAX_REDACTION_DEPTH = 6

# Substring match against normalised key names. Over-redaction is an acceptable
# cost; under-redaction is an incident.
_SENSITIVE_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "authorization",
        "cookie",
        "credential",
        "session_id",
        "signature",
        "otp",
        "cvv",
        "card_number",
    }
)

# Standard LogRecord attributes plus the fields injected by ContextFilter.
# Anything else on the record came from `extra` and is treated as context.
_RESERVED_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "correlation_id",
        "environment",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "request_id",
        "service",
        "span_id",
        "stack_info",
        "stacklevel",
        "taskName",
        "thread",
        "threadName",
        "trace_id",
        "version",
    }
)


def is_sensitive_key(key: str) -> bool:
    """True when a context key name looks like it holds a credential."""
    normalised = key.lower().replace("-", "_")
    return any(fragment in normalised for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any, *, depth: int = 0) -> Any:
    """Recursively replace credential-shaped values with a placeholder."""
    if depth >= _MAX_REDACTION_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, depth=depth + 1) for item in value]
    return value


class ContextFilter(logging.Filter):
    """Injects process identity and correlation ids onto every record.

    Implemented as a filter rather than a formatter concern so that both
    formatters - and any future handler, such as a Sentry or OTLP log handler -
    see the same fields.
    """

    def __init__(self, *, service: str, environment: str, version: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._version = version

    def filter(self, record: logging.LogRecord) -> bool:
        """Populate correlation fields. Always returns True (filters nothing)."""
        trace_id, span_id = current_trace_ids()
        record.__dict__.update(
            {
                "service": self._service,
                "environment": self._environment,
                "version": self._version,
                "request_id": get_request_id(),
                "correlation_id": get_correlation_id(),
                "trace_id": trace_id,
                "span_id": span_id,
            }
        )
        return True


def extract_context(record: logging.LogRecord) -> dict[str, Any]:
    """Return the caller-supplied `extra` fields attached to a record."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
    }


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, suitable for machine ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record as a single-line JSON document."""
        payload: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": record.__dict__.get("service"),
            "environment": record.__dict__.get("environment"),
            "version": record.__dict__.get("version"),
            "request_id": record.__dict__.get("request_id"),
            "correlation_id": record.__dict__.get("correlation_id"),
            "trace_id": record.__dict__.get("trace_id"),
            "span_id": record.__dict__.get("span_id"),
        }

        context = extract_context(record)
        if context:
            payload["context"] = redact(context)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        emitted = {key: value for key, value in payload.items() if value is not None}
        return json.dumps(emitted, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Compact, readable output for local development."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record as a short human-readable line."""
        request_id = record.__dict__.get("request_id")
        prefix = f"[{request_id}] " if request_id else ""
        line = (
            f"{_timestamp(record)} {record.levelname:<8} {record.name} "
            f"{prefix}{record.getMessage()}"
        )

        context = extract_context(record)
        if context:
            line = f"{line} {json.dumps(redact(context), default=str, ensure_ascii=False)}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(settings: Settings) -> None:
    """Install the root logging handler.

    Idempotent: existing root handlers are removed first, so repeated calls -
    for example from tests that build several applications - do not duplicate
    output.
    """
    formatter: logging.Formatter = (
        JsonFormatter() if settings.logging.format == "json" else ConsoleFormatter()
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(
        ContextFilter(
            service=settings.service_name,
            environment=settings.environment.value,
            version=settings.service_version,
        )
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.logging.level)

    # Let uvicorn's records flow through the root handler so that every line in
    # the process has the same shape and the same correlation fields.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
