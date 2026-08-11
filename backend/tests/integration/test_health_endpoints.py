"""Liveness and readiness endpoint behaviour."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform.health import HealthCheck, HealthRegistry


async def passing_check() -> None:
    return None


async def failing_check() -> None:
    raise RuntimeError("dependency down")


def registry_of(app: FastAPI) -> HealthRegistry:
    registry = app.state.health_registry
    assert isinstance(registry, HealthRegistry)
    return registry


def test_liveness_reports_process_identity(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["service"] == "omnichannel-api"
    assert body["environment"] == "test"
    assert body["version"]


def test_readiness_passes_with_no_registered_checks(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == []


def test_readiness_is_unavailable_before_startup(app: FastAPI) -> None:
    # No context manager, so the lifespan never runs.
    test_client = TestClient(app, raise_server_exceptions=False)

    assert test_client.get("/health/ready").status_code == 503
    assert test_client.get("/health/ready").json()["status"] == "not_ready"


def test_liveness_succeeds_before_startup(app: FastAPI) -> None:
    test_client = TestClient(app, raise_server_exceptions=False)
    assert test_client.get("/health/live").status_code == 200


def test_readiness_reports_a_failing_critical_dependency(app: FastAPI) -> None:
    registry_of(app).register(HealthCheck(name="postgresql", check=failing_check))

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][0]["name"] == "postgresql"
    assert body["checks"][0]["status"] == "fail"


def test_a_failing_optional_dependency_keeps_the_service_ready(app: FastAPI) -> None:
    registry_of(app).register(HealthCheck(name="cache", check=failing_check, critical=False))

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"][0]["status"] == "fail"


def test_readiness_never_returns_a_dependency_error_message(app: FastAPI) -> None:
    registry_of(app).register(HealthCheck(name="postgresql", check=failing_check))

    with TestClient(app, raise_server_exceptions=False) as test_client:
        body = test_client.get("/health/ready").text

    assert "dependency down" not in body
    assert "RuntimeError" in body


def test_checks_registered_later_appear_without_changing_the_endpoint(app: FastAPI) -> None:
    registry_of(app).register(HealthCheck(name="postgresql", check=passing_check))
    registry_of(app).register(HealthCheck(name="redis", check=passing_check))

    with TestClient(app, raise_server_exceptions=False) as test_client:
        body = test_client.get("/health/ready").json()

    assert [check["name"] for check in body["checks"]] == ["postgresql", "redis"]


def test_health_endpoints_are_not_versioned(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").status_code == 404
