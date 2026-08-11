"""Explicit Celery context propagation without process-global mutable state."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from app.platform.correlation import sanitize_external_id

_TRACEPARENT = re.compile(r"\A00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}\Z")
_tenant_id: ContextVar[str | None] = ContextVar("oc_tenant_id", default=None)
_traceparent: ContextVar[str | None] = ContextVar("oc_traceparent", default=None)


@dataclass(frozen=True, slots=True)
class TaskContext:
    tenant_id: str
    correlation_id: str
    traceparent: str | None = None

    def as_headers(self) -> dict[str, str]:
        headers = {"tenant_id": self.tenant_id, "correlation_id": self.correlation_id}
        if self.traceparent:
            headers["traceparent"] = self.traceparent
        return headers


@dataclass(frozen=True, slots=True)
class TaskContextTokens:
    tenant_id: Token[str | None]
    traceparent: Token[str | None]


def _required_header(headers: Mapping[str, object], name: str) -> str:
    value = headers.get(name)
    if not isinstance(value, str) or sanitize_external_id(value) is None:
        raise ValueError(f"missing or invalid task header: {name}")
    return value


def task_context_from_headers(headers: Mapping[str, object]) -> TaskContext:
    tenant_id = _required_header(headers, "tenant_id")
    correlation_id = _required_header(headers, "correlation_id")
    raw_traceparent = headers.get("traceparent")
    traceparent: str | None = None
    if raw_traceparent is not None:
        if not isinstance(raw_traceparent, str) or not _TRACEPARENT.fullmatch(raw_traceparent):
            raise ValueError("invalid task header: traceparent")
        traceparent = raw_traceparent
    return TaskContext(tenant_id, correlation_id, traceparent)


def bind_task_context(context: TaskContext) -> TaskContextTokens:
    return TaskContextTokens(
        _tenant_id.set(context.tenant_id), _traceparent.set(context.traceparent)
    )


def reset_task_context(tokens: TaskContextTokens) -> None:
    _tenant_id.reset(tokens.tenant_id)
    _traceparent.reset(tokens.traceparent)


@contextmanager
def task_context(context: TaskContext) -> Iterator[TaskContext]:
    tokens = bind_task_context(context)
    try:
        yield context
    finally:
        reset_task_context(tokens)


def current_tenant_id() -> str | None:
    return _tenant_id.get()


def current_traceparent() -> str | None:
    return _traceparent.get()
