"""Request correlation primitives.

Two identifiers travel with every unit of work:

``request_id``
    Identifies one HTTP request - a single hop. Always present.

``correlation_id``
    Identifies a logical workflow that may span several requests, background
    tasks and provider calls. Defaults to the ``request_id`` when the caller
    does not supply one.

Both live in context variables rather than on the request object, so that code
with no access to the HTTP layer - a future Celery task, a repository, a
provider adapter - can still emit correlated logs. `docs/observability.md`
describes the full correlation set.

Security: inbound identifiers are attacker-controlled. `sanitize_external_id`
enforces a strict charset and length so a caller cannot inject newlines or
terminal escapes into logs, smuggle values into response headers, or bloat
every log line with megabytes of text.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

MIN_ID_LENGTH = 8
MAX_ID_LENGTH = 128

# Deliberately narrow: alphanumerics plus a few separators that cover UUIDs,
# hex strings, W3C trace ids and typical proxy-generated request ids. No
# whitespace, no control characters, no percent-encoding.
_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]+\Z")

_request_id_var: ContextVar[str | None] = ContextVar("oc_request_id", default=None)
_correlation_id_var: ContextVar[str | None] = ContextVar("oc_correlation_id", default=None)


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    """The identifiers bound to the current unit of work."""

    request_id: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class CorrelationTokens:
    """Opaque handles used to restore the previous context on unbind."""

    request_id: Token[str | None]
    correlation_id: Token[str | None]


def new_id() -> str:
    """Generate a fresh identifier."""
    return uuid.uuid4().hex


def is_valid_external_id(value: str | None) -> bool:
    """Return True when an externally supplied identifier is safe to reuse."""
    if value is None:
        return False
    if not MIN_ID_LENGTH <= len(value) <= MAX_ID_LENGTH:
        return False
    return _ID_PATTERN.match(value) is not None


def sanitize_external_id(value: str | None) -> str | None:
    """Return the identifier when it is safe to reuse, otherwise None."""
    return value if is_valid_external_id(value) else None


def resolve_correlation_ids(
    *,
    inbound_request_id: str | None,
    inbound_correlation_id: str | None,
    trust_inbound_request_id: bool,
) -> CorrelationIds:
    """Decide the identifiers for a unit of work.

    An inbound ``request_id`` is honoured only when the deployment explicitly
    trusts it, because it identifies *this* hop and a client should not be able
    to make two unrelated requests share one identity. That flag is off by
    default and is intended to be enabled once a reverse proxy is the only
    ingress and always overwrites the header.

    An inbound ``correlation_id`` is honoured whenever it passes validation:
    joining a caller's existing workflow is precisely its purpose.
    """
    request_id: str | None = None
    if trust_inbound_request_id:
        request_id = sanitize_external_id(inbound_request_id)
    if request_id is None:
        request_id = new_id()

    correlation_id = sanitize_external_id(inbound_correlation_id) or request_id
    return CorrelationIds(request_id=request_id, correlation_id=correlation_id)


def bind(ids: CorrelationIds) -> CorrelationTokens:
    """Bind identifiers to the current context."""
    return CorrelationTokens(
        request_id=_request_id_var.set(ids.request_id),
        correlation_id=_correlation_id_var.set(ids.correlation_id),
    )


def unbind(tokens: CorrelationTokens) -> None:
    """Restore the context that existed before the matching `bind` call."""
    _request_id_var.reset(tokens.request_id)
    _correlation_id_var.reset(tokens.correlation_id)


def get_request_id() -> str | None:
    """Return the current request id, if one is bound."""
    return _request_id_var.get()


def get_correlation_id() -> str | None:
    """Return the current correlation id, if one is bound."""
    return _correlation_id_var.get()
