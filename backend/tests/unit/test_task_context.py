"""Task context is validated, propagated, and always reset."""

import pytest

from app.platform.task_context import (
    TaskContext,
    current_tenant_id,
    current_traceparent,
    task_context,
    task_context_from_headers,
)

TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def test_headers_round_trip() -> None:
    original = TaskContext("tenant-1", "correlation-1", TRACEPARENT)
    assert task_context_from_headers(original.as_headers()) == original


@pytest.mark.parametrize("missing", ["tenant_id", "correlation_id"])
def test_required_headers_cannot_be_missing(missing: str) -> None:
    headers = {"tenant_id": "tenant-1", "correlation_id": "correlation-1"}
    headers.pop(missing)
    with pytest.raises(ValueError, match=missing):
        task_context_from_headers(headers)


def test_traceparent_is_validated() -> None:
    with pytest.raises(ValueError, match="traceparent"):
        task_context_from_headers(
            {"tenant_id": "tenant-1", "correlation_id": "correlation-1", "traceparent": "bad"}
        )


def test_context_is_scoped_and_reset_after_success() -> None:
    with task_context(TaskContext("tenant-1", "correlation-1", TRACEPARENT)):
        assert current_tenant_id() == "tenant-1"
        assert current_traceparent() == TRACEPARENT
    assert current_tenant_id() is None
    assert current_traceparent() is None


def test_context_is_reset_after_failure() -> None:
    with pytest.raises(RuntimeError), task_context(TaskContext("tenant-1", "correlation-1")):
        raise RuntimeError("boom")
    assert current_tenant_id() is None
