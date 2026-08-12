"""The identity audit trail.

Every security-relevant attempt - successful or not - lands in `audit_logs`
through this service. Writing failures matters more than writing successes: a
trail that only records what worked cannot show you an attack in progress.

Rows are written in the caller's transaction. If the operation being audited
rolls back, its audit row rolls back with it, so the trail never claims
something happened that did not. Authentication *failures* are the deliberate
exception - they are committed even though nothing else changed.

The `context` column is scrubbed on the way in. Callers should not be passing
credential material, but "should not" is not a control, and an audit table is
exactly the kind of long-lived, widely-read store where a leaked secret would
sit unnoticed for years.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import models
from app.modules.identity.domain import AuditOutcome
from app.platform.correlation import get_correlation_id, get_request_id

REDACTED: Final = "[redacted]"

# Substring match, not equality: `new_password`, `api_key_secret` and
# `refreshToken` all have to be caught, and an allowlist would fail open for
# every field somebody adds later.
_SENSITIVE_MARKERS: Final = (
    "pass",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
    "digest",
    "hash",
    "apikey",
    "api_key",
)

_MAX_VALUE_LENGTH: Final = 512


def scrub_context(context: Mapping[str, object]) -> dict[str, object]:
    """Return a copy with credential-shaped keys redacted and values bounded."""
    scrubbed: dict[str, object] = {}
    for key, value in context.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            scrubbed[key] = REDACTED
            continue
        if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
            scrubbed[key] = value[:_MAX_VALUE_LENGTH]
            continue
        scrubbed[key] = value
    return scrubbed


class AuditService:
    """Appends rows to `audit_logs`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        tenant_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        actor_api_key_id: uuid.UUID | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> models.AuditLog:
        """Append one audit row and return it.

        Correlation and request identifiers are read from the ambient context
        rather than passed in, so an audit row can always be joined to the log
        lines and the trace for the same request without every call site
        remembering to thread them through.
        """
        record = models.AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_api_key_id=actor_api_key_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome.value,
            correlation_id=get_correlation_id(),
            request_id=get_request_id(),
            ip_address=ip_address,
            context=scrub_context(context or {}),
        )
        self._session.add(record)
        await self._session.flush()
        return record
