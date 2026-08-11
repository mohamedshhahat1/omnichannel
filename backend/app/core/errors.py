"""Application error taxonomy.

These exceptions are transport-agnostic on purpose: services raise them, and
`app.api.exception_handlers` is the single place that turns them into HTTP
responses. A Celery task in a later phase can raise and catch the same types
without dragging in FastAPI.

Every error separates what the client may see from what only the server may
see:

``code``
    Stable, machine-readable identifier. Clients may branch on it, so treat it
    as part of the public API contract.
``message``
    Safe, human-readable sentence intended for the client.
``details``
    Optional structured context. Must be non-sensitive: it is serialised.
``internal_message``
    Operator-facing context. Logged, never serialised.

Only generic, transport-level errors belong here. Domain errors live in the
module that owns the rule and should subclass the closest type below.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """Base class for every deliberately raised application error."""

    default_code: ClassVar[str] = "internal_error"
    default_http_status: ClassVar[int] = 500
    default_message: ClassVar[str] = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        internal_message: str | None = None,
    ) -> None:
        self.code: str = code or self.default_code
        self.http_status: int = http_status or self.default_http_status
        self.message: str = message or self.default_message
        self.details: dict[str, Any] | None = details
        self.internal_message: str | None = internal_message
        super().__init__(self.message)


class BadRequestError(AppError):
    """The request was malformed or semantically invalid."""

    default_code = "bad_request"
    default_http_status = 400
    default_message = "The request was invalid."


class UnauthorizedError(AppError):
    """Authentication is missing or invalid."""

    default_code = "unauthorized"
    default_http_status = 401
    default_message = "Authentication is required."


class ForbiddenError(AppError):
    """Authenticated, but not permitted to perform this action."""

    default_code = "forbidden"
    default_http_status = 403
    default_message = "You do not have access to this resource."


class NotFoundError(AppError):
    """The requested resource does not exist, or is not visible to the caller."""

    default_code = "not_found"
    default_http_status = 404
    default_message = "The requested resource was not found."


class ConflictError(AppError):
    """The request conflicts with the current state of the resource."""

    default_code = "conflict"
    default_http_status = 409
    default_message = "The request conflicts with the current state."


class PayloadTooLargeError(AppError):
    """The request body exceeded the configured limit."""

    default_code = "payload_too_large"
    default_http_status = 413
    default_message = "The request body is too large."


class UnprocessableEntityError(AppError):
    """The request was well-formed but failed validation."""

    default_code = "unprocessable_entity"
    default_http_status = 422
    default_message = "The request could not be processed."


class RateLimitedError(AppError):
    """The caller has exceeded a rate limit."""

    default_code = "rate_limited"
    default_http_status = 429
    default_message = "Too many requests. Please retry later."


class InternalError(AppError):
    """An unexpected server-side failure."""

    default_code = "internal_error"
    default_http_status = 500
    default_message = "An unexpected error occurred."


class ServiceUnavailableError(AppError):
    """A dependency is unavailable and the request cannot be served now."""

    default_code = "service_unavailable"
    default_http_status = 503
    default_message = "The service is temporarily unavailable."
