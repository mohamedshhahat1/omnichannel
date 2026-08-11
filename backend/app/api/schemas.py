"""Shared API response models.

The error envelope defined here is a public contract (ADR-0013). Every error
response the API produces - validation failure, application error, unhandled
exception, or a rejection raised in middleware - has the same shape, so clients
can write one error handler.

Business response models belong to the module that owns them, not here.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.platform.correlation import get_correlation_id, get_request_id


class ErrorDetail(BaseModel):
    """The body of an error response."""

    code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable description, safe to display.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context. Never contains sensitive data.",
    )
    request_id: str | None = Field(default=None, description="Identifier for this request.")
    correlation_id: str | None = Field(default=None, description="Identifier for the workflow.")


class ErrorResponse(BaseModel):
    """Envelope returned for every non-success response."""

    error: ErrorDetail


def build_error_envelope(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a serialisable error envelope for the current request.

    Correlation identifiers are read from the ambient context rather than
    passed in, so no call site can forget them.
    """
    response = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
        )
    )
    return response.model_dump(exclude_none=True)


class HealthCheckResult(BaseModel):
    """Outcome of a single dependency check."""

    name: str
    status: Literal["pass", "fail"]
    critical: bool
    duration_ms: float
    detail: str | None = Field(
        default=None,
        description="Failure category. Never contains an exception message.",
    )


class LivenessResponse(BaseModel):
    """Response of the liveness probe."""

    status: Literal["alive"] = "alive"
    service: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """Response of the readiness probe."""

    status: Literal["ready", "not_ready"]
    service: str
    version: str
    environment: str
    checks: list[HealthCheckResult] = Field(default_factory=list)
