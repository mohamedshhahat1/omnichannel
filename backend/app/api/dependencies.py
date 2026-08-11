"""Shared FastAPI dependencies.

Dependencies are the seam between the HTTP layer and everything else. Phase 1
provided only the three the foundation needed. Phase 2 adds the database
session factory and Redis client here, and route signatures can pick them up
without changing shape.

Resolution reads from `request.app.state` rather than from module-level
globals, so a test can build an isolated application with its own settings and
its own health registry and nothing leaks between them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import InternalError
from app.core.settings import Settings, get_settings
from app.platform.correlation import get_correlation_id, get_request_id
from app.platform.health import HealthRegistry


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identifiers describing the unit of work behind the current request."""

    request_id: str | None
    correlation_id: str | None


def provide_settings(request: Request) -> Settings:
    """Return the settings bound to this application."""
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


def provide_request_context() -> RequestContext:
    """Return the correlation identifiers bound to the current request."""
    return RequestContext(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
    )


def provide_health_registry(request: Request) -> HealthRegistry:
    """Return the application's health check registry.

    A missing registry means the application was not built by `create_app`.
    That is a programming error, not a client error, so it surfaces as 500.
    """
    registry = getattr(request.app.state, "health_registry", None)
    if not isinstance(registry, HealthRegistry):
        raise InternalError(
            internal_message="health_registry is missing from application state.",
        )
    return registry


def provide_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the process-owned SQLAlchemy async session factory."""
    factory = getattr(request.app.state, "session_factory", None)
    if not isinstance(factory, async_sessionmaker):
        raise InternalError(
            internal_message="database session factory is unavailable.",
        )
    return factory


async def provide_database_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(provide_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped async session."""
    async with factory() as session:
        yield session


def provide_redis(request: Request) -> Redis:
    """Return the process-owned Redis client."""
    client = getattr(request.app.state, "redis", None)
    if not isinstance(client, Redis):
        raise InternalError(
            internal_message="redis client is unavailable.",
        )
    return client


SettingsDep = Annotated[Settings, Depends(provide_settings)]
RequestContextDep = Annotated[RequestContext, Depends(provide_request_context)]
HealthRegistryDep = Annotated[HealthRegistry, Depends(provide_health_registry)]
DatabaseSessionDep = Annotated[AsyncSession, Depends(provide_database_session)]
RedisDep = Annotated[Redis, Depends(provide_redis)]
