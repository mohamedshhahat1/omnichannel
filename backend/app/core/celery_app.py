"""Celery factory, queue policy, and task-context enforcement."""

from __future__ import annotations

from typing import Any

from celery import Celery, Task
from kombu import Exchange, Queue

from app.core.settings import Settings, get_settings
from app.platform.correlation import CorrelationIds, bind, unbind
from app.platform.task_context import task_context, task_context_from_headers


class ContextTask(Task):
    """Require tenant/correlation headers and bind them only for this task call."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        headers = self.request.headers or {}
        context = task_context_from_headers(headers)
        correlation_tokens = bind(CorrelationIds(context.correlation_id, context.correlation_id))
        try:
            with task_context(context):
                return self.run(*args, **kwargs)
        finally:
            unbind(correlation_tokens)


def create_celery_app(settings: Settings | None = None) -> Celery:
    settings = settings or get_settings()
    config = settings.celery
    app = Celery("omnichannel", broker=config.broker_url, task_cls=ContextTask)
    app.conf.update(
        result_backend=config.result_backend_url if config.result_backend_enabled else None,
        task_ignore_result=not config.result_backend_enabled,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_track_started=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=config.worker_prefetch_multiplier,
        task_soft_time_limit=config.task_soft_time_limit_seconds,
        task_time_limit=config.task_time_limit_seconds,
        task_default_queue=config.default_queue,
        task_default_exchange=config.default_queue,
        task_default_routing_key=config.default_queue,
        task_queues=(
            Queue(
                config.critical_queue,
                Exchange(config.critical_queue),
                routing_key=config.critical_queue,
            ),
            Queue(
                config.default_queue,
                Exchange(config.default_queue),
                routing_key=config.default_queue,
            ),
            Queue(
                config.background_queue,
                Exchange(config.background_queue),
                routing_key=config.background_queue,
            ),
        ),
        task_routes={
            "app.core.tasks.infrastructure_smoke": {"queue": config.default_queue},
        },
        beat_schedule={},
        imports=("app.core.tasks",),
    )
    return app
