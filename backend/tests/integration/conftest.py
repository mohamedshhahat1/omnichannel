"""Opt-in fixtures for real PostgreSQL and Redis; SQLite is forbidden."""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def _required_url(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not set; Phase 2 integration tests are opt-in")
    return value


@pytest.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    url = _required_url("OC_TEST_DATABASE_URL")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail(
            "OC_TEST_DATABASE_URL must use real PostgreSQL via asyncpg; SQLite is forbidden"
        )
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = _required_url("OC_TEST_REDIS_URL")
    if not url.startswith(("redis://", "rediss://")):
        pytest.fail("OC_TEST_REDIS_URL must be a Redis URL")
    client = Redis.from_url(url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
