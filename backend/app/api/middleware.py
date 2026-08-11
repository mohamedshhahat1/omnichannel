"""ASGI middleware.

These are written as raw ASGI middleware rather than subclasses of Starlette's
`BaseHTTPMiddleware`. `BaseHTTPMiddleware` runs the downstream application in a
separate task, which makes context variable propagation fragile - and context
variables are exactly how correlation identifiers reach the logging layer. Raw
ASGI middleware also avoids buffering the response body.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from starlette.datastructures import Headers, MutableHeaders

from app.api.schemas import build_error_envelope
from app.core.errors import PayloadTooLargeError
from app.platform.correlation import bind, resolve_correlation_ids, unbind

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from app.core.settings import Settings


class CorrelationMiddleware:
    """Binds `request_id` and `correlation_id` for the lifetime of a request.

    The identifiers are bound to the context so that any code reached during
    the request can log them, exposed on `request.state` for handlers that want
    them explicitly, and echoed in the response headers so a client - or an
    operator reading a browser network tab - can quote the id in a bug report.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self._request_header = settings.server.request_id_header
        self._correlation_header = settings.server.correlation_id_header
        self._trust_inbound_request_id = settings.server.trust_inbound_request_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind identifiers, delegate downstream, then restore the context."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        ids = resolve_correlation_ids(
            inbound_request_id=headers.get(self._request_header),
            inbound_correlation_id=headers.get(self._correlation_header),
            trust_inbound_request_id=self._trust_inbound_request_id,
        )

        scope.setdefault("state", {})
        scope["state"]["request_id"] = ids.request_id
        scope["state"]["correlation_id"] = ids.correlation_id

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.append(self._request_header, ids.request_id)
                response_headers.append(self._correlation_header, ids.correlation_id)
            await send(message)

        tokens = bind(ids)
        try:
            await self.app(scope, receive, send_with_correlation)
        finally:
            unbind(tokens)


class SecurityHeadersMiddleware:
    """Adds conservative security headers to every response.

    No Content-Security-Policy: this process serves an API, not documents, and
    a CSP belongs to whatever renders HTML. HSTS is emitted only in production,
    because sending it over plain HTTP in development would pin a browser to a
    scheme the local server does not speak.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self._enabled = settings.security.security_headers_enabled
        self._headers = _build_security_headers(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate downstream and decorate the response headers."""
        if not self._enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self._headers:
                    if name not in response_headers:
                        response_headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class MaxBodySizeMiddleware:
    """Rejects request bodies larger than the configured limit.

    Defence in depth. The authoritative limit belongs at the reverse proxy
    (Phase 2), which can refuse a body before it reaches Python at all. This
    guard exists so the limit still holds when the application is reached
    directly, and so the rejection is a well-formed error envelope rather than
    a memory spike.

    Both a declared `Content-Length` and the actual streamed byte count are
    checked, because the first is a claim and the second is a fact.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the body size limit for the duration of the request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            await self._reject(send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise PayloadTooLargeError(
                        internal_message=f"Body exceeded {self._max_bytes} bytes."
                    )
            return message

        await self.app(scope, counting_receive, send)

    async def _reject(self, send: Send) -> None:
        error = PayloadTooLargeError()
        envelope = build_error_envelope(code=error.code, message=error.message)
        body = json.dumps(envelope).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": error.http_status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _build_security_headers(settings: Settings) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Permissions-Policy", "accelerometer=(), camera=(), geolocation=(), microphone=()"),
        ("Cache-Control", "no-store"),
    ]
    if settings.is_production:
        headers.append(
            (
                "Strict-Transport-Security",
                f"max-age={settings.security.hsts_max_age_seconds}; includeSubDomains",
            )
        )
    return tuple(headers)
