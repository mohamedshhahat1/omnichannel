"""Async SQLAlchemy engine, sessions, metadata, and health checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.settings import DatabaseSettings

NAMING_CONVENTION = {
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "uq": "%(table_name)s_%(column_0_N_name)s_uq",
    "ck": "%(table_name)s_%(constraint_name)s_ck",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "%(table_name)s_pk",
}


class Base(DeclarativeBase):
    """Declarative base for future module-owned models; Phase 2 adds no models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


SessionFactory = async_sessionmaker[AsyncSession]


def create_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.url,
        pool_pre_ping=True,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        pool_recycle=settings.pool_recycle_seconds,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.statement_timeout_ms),
                "lock_timeout": str(settings.lock_timeout_ms),
                "timezone": "UTC",
                "application_name": "omnichannel-api",
            }
        },
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Commit on success and roll back on failure."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_database_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
