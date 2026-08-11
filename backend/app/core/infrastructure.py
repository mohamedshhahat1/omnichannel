"""Application-owned infrastructure lifecycle and health registration."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.database import (
    create_database_engine,
    create_session_factory,
    dispose_database_engine,
    ping_database,
)
from app.core.redis import close_redis_client, create_redis_client, ping_redis
from app.core.settings import Settings
from app.platform.health import HealthCheck, HealthRegistry


@dataclass(frozen=True, slots=True)
class Infrastructure:
    database_engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis


async def start_infrastructure(settings: Settings, registry: HealthRegistry) -> Infrastructure:
    engine = create_database_engine(settings.database)
    redis = create_redis_client(settings.redis)
    registry.register(
        HealthCheck(
            name="postgresql",
            check=lambda: ping_database(engine),
            timeout_seconds=settings.database.health_timeout_seconds,
        )
    )
    registry.register(
        HealthCheck(
            name="redis",
            check=lambda: ping_redis(redis),
            timeout_seconds=settings.redis.health_timeout_seconds,
        )
    )
    return Infrastructure(engine, create_session_factory(engine), redis)


async def stop_infrastructure(infrastructure: Infrastructure) -> None:
    try:
        await close_redis_client(infrastructure.redis)
    finally:
        await dispose_database_engine(infrastructure.database_engine)
