"""The single source of wall-clock time.

Ruff's DTZ rules already ban naive datetimes. Funnelling every read through one
function adds the other half of that guarantee: expiry, rotation and lockout
logic all read the same clock, and a test can substitute it in one place
instead of patching `datetime` in a dozen modules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def seconds_from_now(seconds: int) -> datetime:
    """Return the UTC instant `seconds` in the future."""
    return utcnow() + timedelta(seconds=seconds)


def is_expired(deadline: datetime | None, *, now: datetime | None = None) -> bool:
    """Return True when `deadline` is in the past.

    A `None` deadline means "never expires", which is why this helper exists:
    the alternative is a `None` check at every call site, and the one that gets
    forgotten is the one that lets a revoked credential through.
    """
    if deadline is None:
        return False
    return deadline <= (now or utcnow())
