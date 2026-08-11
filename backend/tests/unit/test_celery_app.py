"""Celery has the approved queues and no default result backend."""

import pytest

from app.core.celery_app import ContextTask, create_celery_app
from app.core.settings import Environment, Settings


def settings() -> Settings:
    return Settings(_env_file=None, environment=Environment.TEST)


def test_factory_configures_three_queues() -> None:
    app = create_celery_app(settings())
    assert {queue.name for queue in app.conf.task_queues} == {
        "critical",
        "default",
        "background",
    }
    assert app.conf.task_default_queue == "default"


def test_results_are_disabled_by_default() -> None:
    app = create_celery_app(settings())
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None


def test_delivery_safety_settings_are_enabled() -> None:
    app = create_celery_app(settings())
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1


def test_context_task_rejects_missing_headers() -> None:
    task = ContextTask()
    task.run = lambda: None  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="tenant_id"):
        task()
