"""Effective-permission resolution, with a Redis cache in front of PostgreSQL.

Runtime authorization reads the database. A membership's effective permission
set is resolved by walking `membership_roles -> roles -> role_permissions ->
permissions`, so changing what a role grants is a data change that takes effect
on the next resolution rather than a code change and a redeploy.

`DEFAULT_ROLE_GRANTS` in `domain` is the *seed* for those rows and the reference
matrix the unit tests assert against. It is deliberately not imported here: if
it were, a grant edited in the database would be silently overruled by a Python
constant, which is exactly the defect this module was corrected to remove.

The cache uses `app.core.redis.tenant_key`, which is the existing tenant-safe
namespace - and it can, because this lookup happens *after* the tenant has been
established from the session or API key. Nothing before that point is cached in
Redis, which is why sessions themselves stay in PostgreSQL (ADR-0009): their
lookup key is a bare token digest with no tenant to scope it to.

The cache is advisory in both directions. A Redis outage degrades to a database
read rather than an authorization failure, and the TTL is short and bounded by
`AuthSettings.session_cache_ttl_seconds`, so a grant change that somehow escapes
explicit invalidation still cannot outlive it by more than a minute.
"""

from __future__ import annotations

import json
import uuid
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.redis import tenant_key
from app.modules.identity.domain import Permission
from app.modules.identity.repositories import MembershipRepository

# Deliberately not the "rbac" namespace used while role slugs were cached. An
# entry written by the previous implementation holds role slugs; reading one
# back as an effective permission set would be a silent authorization change,
# so the two generations of value must never share a key.
_CACHE_PURPOSE: Final = "rbac-perms"
_ROLES_FIELD: Final = "r"
_PERMISSIONS_FIELD: Final = "p"


class EffectivePermissionCache:
    """Read-through cache of what one membership may currently do.

    The cached value is the effective permission set already resolved from
    PostgreSQL, together with the role slugs it was resolved through. Caching
    the permissions rather than the role slugs is what keeps Redis a pure
    performance layer: no reader has to re-derive a decision from a slug, so
    there is no second code path that could disagree with the database.
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
        """Return the tenant-scoped key for one membership.

        The tenant id is a structural part of the key, not a field inside the
        value, so an entry belonging to one tenant is not addressable from
        another even if a membership id were reused or guessed.
        """
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
    ) -> tuple[frozenset[str], frozenset[str]] | None:
        """Return the cached (role slugs, permission slugs), or None on a miss.

        Every unreadable, malformed or unexpectedly shaped entry is reported as
        a miss. A bad cache value can therefore only ever cost a database read;
        it can never widen what a principal is allowed to do.
        """
        if self._redis is None or not self.enabled:
            return None
        try:
            raw: object = await self._redis.get(self._key(tenant_id, membership_id))
        except RedisError:
            return None
        if not isinstance(raw, str):
            return None
        try:
            payload: object = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        roles = payload.get(_ROLES_FIELD)
        permissions = payload.get(_PERMISSIONS_FIELD)
        if not isinstance(roles, list) or not isinstance(permissions, list):
            return None
        if not all(isinstance(item, str) for item in roles):
            return None
        if not all(isinstance(item, str) for item in permissions):
            return None
        return (
            frozenset(item for item in roles if isinstance(item, str)),
            frozenset(item for item in permissions if isinstance(item, str)),
        )

    async def set(
        self,
        tenant_id: uuid.UUID,
        membership_id: uuid.UUID,
        roles: frozenset[str],
        permissions: frozenset[str],
    ) -> None:
        """Store the resolved sets under the tenant-scoped key.

        A membership that resolves to nothing is stored explicitly rather than
        skipped. Without that, "resolves to no permissions" and "not cached"
        would be the same observation, and the least-privileged accounts would
        be the only ones that never benefit from the cache.
        """
        if self._redis is None or not self.enabled:
            return
        payload = json.dumps(
            {
                _ROLES_FIELD: sorted(roles),
                _PERMISSIONS_FIELD: sorted(permissions),
            },
            separators=(",", ":"),
        )
        try:
            await self._redis.setex(
                self._key(tenant_id, membership_id),
                self._ttl_seconds,
                payload,
            )
        except RedisError:
            return

    async def invalidate(self, tenant_id: uuid.UUID, membership_id: uuid.UUID) -> None:
        """Drop the cached entry after an RBAC change. Failures are ignored."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(tenant_id, membership_id))
        except RedisError:
            return


class PermissionResolver:
    """Turns a membership into the authority it currently holds.

    Resolution order is Redis, then PostgreSQL. PostgreSQL is authoritative;
    Redis only ever replays an answer PostgreSQL already gave.
    """

    def __init__(
        self,
        memberships: MembershipRepository,
        cache: EffectivePermissionCache,
    ) -> None:
        self._memberships = memberships
        self._cache = cache

    async def _resolve(
        self,
        membership_id: uuid.UUID,
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Return (role slugs, permission slugs) from the cache or the database.

        Both sets come from one query and are cached as one entry, so the roles
        a caller is reported to hold and the permissions they are granted can
        never be a generation apart.
        """
        tenant_id = self._memberships.tenant_id
        cached = await self._cache.get(tenant_id, membership_id)
        if cached is not None:
            return cached
        roles, permissions = await self._memberships.role_and_permission_slugs_for(
            membership_id,
        )
        await self._cache.set(tenant_id, membership_id, roles, permissions)
        return roles, permissions

    async def role_slugs_for(self, membership_id: uuid.UUID) -> frozenset[str]:
        """Return the membership's role slugs.

        Informational only - these name the roles a membership holds, they do
        not decide anything. `permissions_for` is the authorization answer.
        """
        roles, _ = await self._resolve(membership_id)
        return roles

    async def permissions_for(self, membership_id: uuid.UUID) -> frozenset[Permission]:
        """Return the effective permission set resolved from PostgreSQL.

        A stored slug outside the `Permission` catalogue is dropped rather than
        raising. An unrecognised grant must fail closed - contributing nothing -
        instead of turning every request from an affected member into a 500.
        """
        _, permission_slugs = await self._resolve(membership_id)
        catalogue = {permission.value: permission for permission in Permission}
        return frozenset(catalogue[slug] for slug in permission_slugs if slug in catalogue)

    async def invalidate(self, membership_id: uuid.UUID) -> None:
        """Forget the cached authority of one membership.

        This is the invalidation hook. Every service that mutates a row which
        affects what a membership may do - a role assignment, a role removal, a
        change to a role's grants - must call it, because the alternative is
        waiting out the TTL while a revoked privilege still works.
        """
        await self._cache.invalidate(self._memberships.tenant_id, membership_id)
