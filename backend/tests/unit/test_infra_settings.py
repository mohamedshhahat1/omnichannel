"""Phase 2 infrastructure settings are typed and environment-driven."""

import pytest
from pydantic import ValidationError

from app.core.settings import CelerySettings, DatabaseSettings, Environment, Settings


def test_infrastructure_defaults_are_explicit() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST)
    assert settings.database.url.startswith("postgresql+asyncpg://")
    assert settings.redis.url.endswith("/0")
    assert settings.celery.broker_url.endswith("/1")
    assert settings.celery.result_backend_enabled is False
    assert settings.celery.result_backend_url is None


def test_nested_environment_values_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC_DATABASE__POOL_SIZE", "9")
    monkeypatch.setenv("OC_REDIS__MAX_CONNECTIONS", "31")
    monkeypatch.setenv("OC_CELERY__BACKGROUND_QUEUE", "slow")
    settings = Settings(_env_file=None)
    assert settings.database.pool_size == 9
    assert settings.redis.max_connections == 31
    assert settings.celery.background_queue == "slow"


def test_database_rejects_sync_driver() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        DatabaseSettings(url="postgresql://localhost/db")


def test_enabled_result_backend_requires_url() -> None:
    with pytest.raises(ValidationError, match="result_backend_url"):
        CelerySettings(result_backend_enabled=True)


def test_celery_limits_and_queue_names_are_validated() -> None:
    with pytest.raises(ValidationError, match="soft time limit"):
        CelerySettings(task_soft_time_limit_seconds=300, task_time_limit_seconds=300)
    with pytest.raises(ValidationError, match="distinct"):
        CelerySettings(default_queue="same", critical_queue="same")
