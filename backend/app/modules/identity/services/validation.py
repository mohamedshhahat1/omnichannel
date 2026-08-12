"""Bridging domain normalisation failures into the error taxonomy.

`domain` raises plain `ValueError` so it stays free of any dependency on the
HTTP layer. Services translate those into `IdentityValidationError`, which the
shared exception handler renders as a 422 in the standard envelope.

The domain messages are written to be safe to show a person - they describe the
rule that was broken and never echo the value that broke it, which matters
because one of those values is a password.
"""

from __future__ import annotations

from collections.abc import Callable

from app.modules.identity.errors import IdentityValidationError


def normalized(field: str, value: str, normalizer: Callable[[str], str]) -> str:
    """Apply a domain normaliser, converting rejection into a 422."""
    try:
        return normalizer(value)
    except ValueError as exc:
        raise IdentityValidationError(str(exc), details={"field": field}) from exc


def enforce(field: str, check: Callable[[], None]) -> None:
    """Run a domain policy check, converting rejection into a 422."""
    try:
        check()
    except ValueError as exc:
        raise IdentityValidationError(str(exc), details={"field": field}) from exc
