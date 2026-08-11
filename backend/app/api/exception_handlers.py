"""Translation from exceptions to HTTP responses.

This is the only place in the codebase that decides what an error looks like on
the wire. Handlers are registered for four cases:

- `AppError` - a deliberate, classified application error.
- `RequestValidationError` - a malformed request body, query or path.
- `StarletteHTTPException` - framework-raised errors such as 404 and 405.
- `Exception` - anything unforeseen.

Two rules apply to all of them: the client never receives a stack trace, an
exception message, or an echo of its own submitted values; and the response
always carries the request id so an operator can find the matching log line.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import build_error_envelope
from app.core.errors import AppError, UnprocessableEntityError
from app.platform.correlation import CorrelationIds, CorrelationTokens, bind, unbind

logger = logging.getLogger(__name__)

_SERVER_ERROR_THRESHOLD = 500

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable_entity",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

_GENERIC_SERVER_MESSAGE = "An unexpected error occurred."


def _json_error(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=build_error_envelope(code=code, message=message, details=details),
    )


def _safe_validation_details(exc: RequestValidationError) -> dict[str, Any]:
    """Summarise a validation failure without echoing submitted values.

    FastAPI's default handler includes the offending `input` in the response.
    For a login or a webhook payload that means reflecting a password or a
    provider secret straight back to the caller - and into any proxy log along
    the way. Only the location, the rule that failed, and a message survive.
    """
    return {
        "fields": [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "type": str(error.get("type", "invalid")),
                "message": str(error.get("msg", "Invalid value.")),
            }
            for error in exc.errors()
        ]
    }


async def app_error_handler(request: Request, exc: Exception) -> Response:
    """Render a deliberate application error."""
    if not isinstance(exc, AppError):
        raise exc

    context = {
        "path": request.url.path,
        "method": request.method,
        "error_code": exc.code,
        "status_code": exc.http_status,
        "internal_message": exc.internal_message,
    }
    if exc.http_status >= _SERVER_ERROR_THRESHOLD:
        logger.error("http.application_error", extra=context, exc_info=exc)
    else:
        logger.info("http.application_error", extra=context)

    return _json_error(
        status_code=exc.http_status,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    """Render a request validation failure as 422."""
    if not isinstance(exc, RequestValidationError):
        raise exc

    details = _safe_validation_details(exc)
    logger.info(
        "http.validation_error",
        extra={
            "path": request.url.path,
            "method": request.method,
            "field_count": len(details["fields"]),
        },
    )
    return _json_error(
        status_code=UnprocessableEntityError.default_http_status,
        code=UnprocessableEntityError.default_code,
        message="The request failed validation.",
        details=details,
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render a framework-raised HTTP error in the standard envelope."""
    if not isinstance(exc, StarletteHTTPException):
        raise exc

    code = _STATUS_CODES.get(exc.status_code, "http_error")
    is_server_error = exc.status_code >= _SERVER_ERROR_THRESHOLD
    message = _GENERIC_SERVER_MESSAGE if is_server_error else str(exc.detail)

    logger.info(
        "http.exception",
        extra={
            "path": request.url.path,
            "method": request.method,
            "status_code": exc.status_code,
        },
    )
    return _json_error(status_code=exc.status_code, code=code, message=message)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Render an unforeseen failure as an opaque 500.

    The exception, its type and its traceback go to the log. The client gets a
    generic sentence and the request id, and nothing else.
    """
    logger.error(
        "http.unhandled_exception",
        extra={
            "path": request.url.path,
            "method": request.method,
            "exception_type": type(exc).__name__,
        },
        exc_info=exc,
    )
    # This handler runs in Starlette's ServerErrorMiddleware, outside the
    # correlation middleware, so the ambient identifiers were already unbound
    # while the exception unwound the stack. Recover them from request.state,
    # where the correlation middleware stores them for exactly this purpose.
    request_id: str | None = getattr(request.state, "request_id", None)
    correlation_id: str | None = getattr(request.state, "correlation_id", None)
    tokens: CorrelationTokens | None = None
    if request_id is not None and correlation_id is not None:
        tokens = bind(CorrelationIds(request_id=request_id, correlation_id=correlation_id))
    try:
        return _json_error(
            status_code=500,
            code="internal_error",
            message=_GENERIC_SERVER_MESSAGE,
        )
    finally:
        if tokens is not None:
            unbind(tokens)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every exception handler to the application."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
