"""Async Redis lifecycle and tenant-safe key construction."""

from __future__ import annotations

import re
from typing import Final

from redis.asyncio import Redis

from app.core.settings import RedisSettings

_SEGMENT: Final = re.compile(r"\A[A-Za-z0-9._-]+\Z")


def _safe_segment(name: str, value: str) -> str:
    if not value or not _SEGMENT.fullmatch(value):
        raise ValueError(f"{name} must contain only letters, digits, dot, underscore, or hyphen")
    return value


def tenant_key(environment: str, tenant_id: str, purpose: str, *parts: str) -> str:
    """Return `oc:{env}:t:{tenant_id}:{purpose}:...` without unsafe separators."""
    segments = [
        "oc",
        _safe_segment("environment", environment),
        "t",
        _safe_segment("tenant_id", tenant_id),
        _safe_segment("purpose", purpose),
    ]
    segments.extend(_safe_segment("key part", part) for part in parts)
    return ":".join(segments)


def create_redis_client(settings: RedisSettings) -> Redis:
    client: Redis = Redis.from_url(
        settings.url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.max_connections,
        socket_connect_timeout=settings.socket_connect_timeout_seconds,
        socket_timeout=settings.socket_timeout_seconds,
        health_check_interval=30,
    )
    return client


async def ping_redis(client: Redis) -> None:
    if await client.ping() is not True:
        raise RuntimeError("Redis PING returned an unexpected response")


async def close_redis_client(client: Redis) -> None:
    await client.aclose()
