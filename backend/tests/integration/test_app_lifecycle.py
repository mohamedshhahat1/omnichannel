"""Application factory, startup and shutdown."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core.settings import Environment, SecuritySettings, Settings
from app.platform.health import HealthRegistry
from tests.conftest import build_settings


def test_factory_wires_application_state(app: FastAPI, settings: Settings) -> None:
    assert app.state.settings is settings
    assert isinstance(app.state.health_registry, HealthRegistry)
    assert app.state.started is False


def test_each_application_gets_its_own_registry(settings: Settings) -> None:
    first = create_app(settings)
    second = create_app(settings)
    assert first.state.health_registry is not second.state.health_registry


def test_startup_and_shutdown_toggle_the_started_flag(app: FastAPI) -> None:
    with TestClient(app) as test_client:
        assert app.state.started is True
        assert test_client.get("/health/ready").status_code == 200

    assert app.state.started is False


def test_routes_are_mounted_where_expected(app: FastAPI, settings: Settings) -> None:
    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert f"{settings.api_prefix}/v1" not in paths


def test_docs_are_available_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_docs_are_disabled_in_production() -> None:
    production = build_settings(
        environment=Environment.PRODUCTION,
        security=SecuritySettings(trusted_hosts=("testserver",)),
    )
    with TestClient(create_app(production), raise_server_exceptions=False) as test_client:
        assert test_client.get("/openapi.json").status_code == 404
        assert test_client.get("/docs").status_code == 404
