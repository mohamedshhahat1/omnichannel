"""Shared test fixtures.

Tests are hermetic: no network, no external services, and no dependence on the
developer's shell environment. `OC_`-prefixed variables are stripped before
every test and settings are built with `_env_file=None`, so a stray export
cannot make the suite pass or fail by accident. The documented `OC_TEST_*`
opt-in integration URLs (see `.env.example`) are the one exception: they are
read directly by `tests/integration/conftest.py`, and stripping them would
silently skip every Phase 2 integration test.
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core.settings import Environment, LoggingSettings, Settings, get_settings


def build_settings(**overrides: Any) -> Settings:
    """Build a deterministic Settings instance for tests."""
    defaults: dict[str, Any] = {
        "environment": Environment.TEST,
        "debug": False,
        "logging": LoggingSettings(level="DEBUG", format="json"),
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove application environment variables and reset the settings cache."""
    for key in list(os.environ):
        # Keep the documented OC_TEST_* opt-in integration URLs; they are not
        # application settings and tests/integration/conftest.py reads them
        # directly via os.getenv.
        if key.startswith("OC_") and not key.startswith("OC_TEST_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Default test settings."""
    return build_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """An application instance that has not yet run its lifespan."""
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client for a started application.

    `raise_server_exceptions=False` makes the client behave like a real HTTP
    client, so the 500 responses produced by the exception handlers can be
    asserted on instead of being re-raised into the test.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
