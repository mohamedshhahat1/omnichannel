"""Infrastructure-only Celery smoke task."""

from __future__ import annotations

from celery import shared_task

from app.platform.task_context import current_tenant_id


@shared_task(
    name="app.core.tasks.infrastructure_smoke",
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def infrastructure_smoke() -> dict[str, str]:
    """Prove worker routing and trusted task context; no business side effect."""
    tenant_id = current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("task context was not bound")
    return {"status": "ok", "tenant_id": tenant_id}
