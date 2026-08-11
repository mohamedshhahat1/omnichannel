"""Health check registry.

The readiness endpoint asks this registry whether the process can serve
traffic. In Phase 1 the registry is empty, so readiness reflects application
startup alone - there is no PostgreSQL or Redis to check yet.

Phase 2 adds infrastructure checks by registering them at startup::

    registry.register(HealthCheck(name="postgresql", check=ping_database))
    registry.register(HealthCheck(name="redis", check=ping_redis, critical=False))

The endpoint itself does not change. That is the whole point of the registry.

Design notes:

- A check is any awaitable that returns normally to pass and raises to fail.
  No return value to interpret, no bespoke result type for callers to build.
- Every check is bounded by its own timeout, so one hung dependency cannot hang
  the readiness probe and get the container killed by mistake.
- Non-critical checks are reported but never fail readiness. Use them for
  dependencies the service can degrade around.
- Failure detail is the exception *type*, never its message. Exception messages
  routinely contain connection strings and credentials, and this payload is
  served over HTTP. The full exception goes to the log instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0

CheckCallable = Callable[[], Awaitable[None]]


class HealthStatus(StrEnum):
    """Outcome of a single check or of the aggregate report."""

    PASS = "pass"  # noqa: S105 - health status literal, not a credential
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A registered dependency check."""

    name: str
    check: CheckCallable
    critical: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The result of running one check."""

    name: str
    status: HealthStatus
    critical: bool
    duration_ms: float
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The aggregate result of running every registered check."""

    status: HealthStatus
    outcomes: tuple[CheckOutcome, ...]


class HealthRegistry:
    """Holds the dependency checks that readiness should evaluate.

    One instance is created per application and stored on the application
    state, rather than as a module-level global, so that tests can build an
    isolated application without leaking registrations between them.
    """

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}

    def register(self, check: HealthCheck) -> None:
        """Register a check. Raises ValueError on a duplicate name."""
        if check.name in self._checks:
            message = f"A health check named {check.name!r} is already registered."
            raise ValueError(message)
        self._checks[check.name] = check

    @property
    def names(self) -> tuple[str, ...]:
        """Names of the registered checks, in registration order."""
        return tuple(self._checks)

    async def run(self) -> HealthReport:
        """Run every check concurrently and aggregate the outcomes."""
        checks = tuple(self._checks.values())
        if not checks:
            return HealthReport(status=HealthStatus.PASS, outcomes=())

        outcomes = await asyncio.gather(*(_run_check(check) for check in checks))
        degraded = any(
            outcome.critical and outcome.status is HealthStatus.FAIL for outcome in outcomes
        )
        status = HealthStatus.FAIL if degraded else HealthStatus.PASS
        return HealthReport(status=status, outcomes=tuple(outcomes))


async def _run_check(check: HealthCheck) -> CheckOutcome:
    started = time.perf_counter()
    detail: str | None = None
    status = HealthStatus.PASS

    try:
        await asyncio.wait_for(check.check(), timeout=check.timeout_seconds)
    except TimeoutError:
        status = HealthStatus.FAIL
        detail = "timeout"
        logger.warning(
            "health.check.timeout",
            extra={"check_name": check.name, "timeout_seconds": check.timeout_seconds},
        )
    except Exception as exc:  # a failing check must not crash readiness
        status = HealthStatus.FAIL
        detail = type(exc).__name__
        logger.warning(
            "health.check.failed",
            extra={"check_name": check.name},
            exc_info=exc,
        )

    duration_ms = (time.perf_counter() - started) * 1000
    return CheckOutcome(
        name=check.name,
        status=status,
        critical=check.critical,
        duration_ms=round(duration_ms, 3),
        detail=detail,
    )
