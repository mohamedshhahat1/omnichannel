"""Health check registry behaviour."""

import asyncio

import pytest

from app.platform.health import HealthCheck, HealthRegistry, HealthStatus

LEAKY_MESSAGE = "postgresql://app:hunter2@db.internal:5432/omnichannel refused"


async def passing_check() -> None:
    return None


async def failing_check() -> None:
    raise RuntimeError(LEAKY_MESSAGE)


async def hanging_check() -> None:
    await asyncio.sleep(5)


def test_empty_registry_reports_healthy() -> None:
    report = asyncio.run(HealthRegistry().run())
    assert report.status is HealthStatus.PASS
    assert report.outcomes == ()


def test_passing_check_is_reported() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=passing_check))

    report = asyncio.run(registry.run())

    assert report.status is HealthStatus.PASS
    assert len(report.outcomes) == 1
    outcome = report.outcomes[0]
    assert outcome.name == "postgresql"
    assert outcome.status is HealthStatus.PASS
    assert outcome.detail is None
    assert outcome.duration_ms >= 0


def test_failing_critical_check_fails_the_report() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=failing_check))

    report = asyncio.run(registry.run())

    assert report.status is HealthStatus.FAIL
    assert report.outcomes[0].status is HealthStatus.FAIL


def test_failing_non_critical_check_does_not_fail_the_report() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="cache", check=failing_check, critical=False))

    report = asyncio.run(registry.run())

    assert report.status is HealthStatus.PASS
    assert report.outcomes[0].status is HealthStatus.FAIL
    assert report.outcomes[0].critical is False


def test_failure_detail_never_exposes_the_exception_message() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=failing_check))

    report = asyncio.run(registry.run())
    detail = report.outcomes[0].detail or ""

    assert detail == "RuntimeError"
    assert "hunter2" not in detail
    assert "postgresql://" not in detail


def test_a_hanging_check_is_bounded_by_its_timeout() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="slow", check=hanging_check, timeout_seconds=0.01))

    report = asyncio.run(registry.run())

    assert report.status is HealthStatus.FAIL
    assert report.outcomes[0].detail == "timeout"


def test_one_failing_check_does_not_hide_the_others() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=failing_check))
    registry.register(HealthCheck(name="redis", check=passing_check))

    report = asyncio.run(registry.run())
    by_name = {outcome.name: outcome.status for outcome in report.outcomes}

    assert by_name == {"postgresql": HealthStatus.FAIL, "redis": HealthStatus.PASS}


def test_registration_order_is_preserved() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=passing_check))
    registry.register(HealthCheck(name="redis", check=passing_check))

    assert registry.names == ("postgresql", "redis")


def test_duplicate_registration_is_rejected() -> None:
    registry = HealthRegistry()
    registry.register(HealthCheck(name="postgresql", check=passing_check))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(HealthCheck(name="postgresql", check=passing_check))


def test_registries_are_isolated_from_each_other() -> None:
    first = HealthRegistry()
    first.register(HealthCheck(name="postgresql", check=passing_check))

    assert HealthRegistry().names == ()
    assert first.names == ("postgresql",)
