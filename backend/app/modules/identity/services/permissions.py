"""Effective-permission resolution, with an optional Redis read-through cache.

Every authenticated request needs the caller's permission set, and deriving it
means joining memberships to roles. That is a small query, but it is a small
query on the hottest path in the system, so the answer is cached.

The cache uses `app.core.redis.tenant_key`, which is the existing tenant-safe
namespace - and it can, because this lookup happens *after* the tenant has been
established from the session or API key. Nothing before that point is cached in
Redis, which is why sessions themselves stay in PostgreSQL (ADR-0009): their
lookup key is a bare token digest with no tenant to scope it to.

The cache is advisory in both directions. A Redis outage degrades to a database
read rather than an authentication failure, and the TTL is short and bounded by
`AuthSettings.session_cache_ttl_seconds`, so a role change that somehow escapes
explicit invalidation still cannot outlive it by more than a minute.
"""

from __future__ import annotations

import uuid
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.redis import tenant_key
from app.modules.identity.domain import Permission, permissions_for_roles
from app.modules.identity.repositories import MembershipRepository

_CACHE_PURPOSE: Final = "rbac"
_EMPTY_SENTINEL: Final = "-"
_SEPARATOR: Final = ","


class RoleSlugCache:
    """Read-through cache of the role slugs held by one membership.

    Role slugs are cached rather than permission names because the slug set is
    the smaller, more stable value: changing what a role grants is a code and
    migration change, while who holds which role changes at runtime.
    """

    def __init__(
        self,
        redis: Redis | None,
        *,
        environment: str,
        ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._environment = environment
        self._ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        """True when a client is available and the TTL is positive."""
        return self._redis is not None and self._ttl_seconds > 0

    def _key(self, tenant_id: uuid.UUID, membership_id: uuid.UUID) -> str:
        return tenant_key(
            self._environment,
            str(tenant_id),
            _CACHE_PURPOSE,
            str(membership_id),
        )

    async def get(
        self,
        tenant_id: uuid.UUID,
        membership_id: uuid.UUID,
    ) -> frozenset[str] | None:
        """Return cached slugs, or None on a miss or any Redis problem."""
        if self._redis is None or not self.enabled:
            return None
        try:
            raw: object = await self._redis.get(self._key(tenant_id, membership_id))
        except RedisError:
            return None
        if not isinstance(raw, str):
            return None
        if raw == _EMPTY_SENTINEL:
            return frozenset()
        return frozenset(part for part in raw.split(_SEPARATOR) if part)

    async def set(
        self,
        tenant_id: uuid.UUID,
        membership_id: uuid.UUID,
        slugs: frozenset[str],
    ) -> None:
        """Store slugs under the tenant-scoped key. Failures are ignored.

        A membership with no roles is stored as an explicit sentinel. Without
        it, "no roles" and "not cached" would be the same value and the
        least-privileged accounts would be the only ones that never benefit
        from the cache.
        """
        if self._redis is None or not self.enabled:
            return
        payload = _SEPARATOR.join(sorted(slugs)) if slugs else _EMPTY_SENTINEL
        try:
            await self._redis.setex(
                self._key(tenant_id, membership_id),
                self._ttl_seconds,
                payload,
            )
        except RedisError:
            return

    async def invalidate(self, tenant_id: uuid.UUID, membership_id: uuid.UUID) -> None:
        """Drop the cached entry after a role change. Failures are ignored."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(tenant_id, membership_id))
        except RedisError:
            return


class PermissionResolver:
    """Turns a membership into the permission set it currently confers."""

    def __init__(self, memberships: MembershipRepository, cache: RoleSlugCache) -> None:
        self._memberships = memberships
        self._cache = cache

    async def role_slugs_for(self, membership_id: uuid.UUID) -> frozenset[str]:
        """Return the membership's role slugs, using the cache when possible."""
        tenant_id = self._memberships.tenant_id
        cached = await self._cache.get(tenant_id, membership_id)
        if cached is not None:
            return cached
        slugs = await self._memberships.role_slugs_for(membership_id)
        await self._cache.set(tenant_id, membership_id, slugs)
        return slugs

    async def permissions_for(self, membership_id: uuid.UUID) -> frozenset[Permission]:
        """Return the effective permission set for a membership."""
        return permissions_for_roles(await self.role_slugs_for(membership_id))

    async def invalidate(self, membership_id: uuid.UUID) -> None:
        """Forget any cached slugs for a membership."""
        await self._cache.invalidate(self._memberships.tenant_id, membership_id)
