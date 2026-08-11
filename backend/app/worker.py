"""Celery worker and beat entrypoint: `celery -A app.worker:celery_app worker`."""

from app.core.celery_app import create_celery_app
from app.core.logging import configure_logging
from app.core.settings import get_settings

settings = get_settings()
configure_logging(settings)
celery_app = create_celery_app(settings)
celery_app.autodiscover_tasks(["app.core"])
