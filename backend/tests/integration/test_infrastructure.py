"""Real-service smoke tests; skipped unless explicit test URLs are supplied."""

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgresql_connection(postgres_engine: AsyncEngine) -> None:
    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
        assert await connection.scalar(text("SELECT current_setting('TimeZone')"))


@pytest.mark.asyncio
async def test_required_extensions_are_available(postgres_engine: AsyncEngine) -> None:
    async with postgres_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT extname FROM pg_extension "
                "WHERE extname IN ('pgcrypto', 'pg_stat_statements', 'pg_trgm', 'vector')"
            )
        )
        assert {row[0] for row in rows} == {
            "pgcrypto",
            "pg_stat_statements",
            "pg_trgm",
            "vector",
        }


@pytest.mark.asyncio
async def test_redis_connection(redis_client: Redis) -> None:
    assert await redis_client.ping() is True
