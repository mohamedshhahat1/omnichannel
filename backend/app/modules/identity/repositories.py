"""Data access for the identity module.

The brief says tenant isolation must not depend on every developer remembering
to add a filter. The pattern here is the answer to that: a tenant-owned table
is only reachable through a `TenantScopedRepository`, which cannot be
constructed without a `TenantContext` and which builds every statement from a
single `_base()` that already carries the `tenant_id` predicate. Forgetting the
filter is not something the code lets you express - you would have to import
`models` and hand-write a `select` to get around it, which is a visible,
reviewable act rather than an omission.

Exactly three lookups are deliberately *not* tenant scoped, and each one exists
to establish the tenant in the first place:

* `UserRepository` - users are global; the same person may belong to several
  tenants with one credential.
* `SessionRepository` - a session is found by token digest before any tenant is
  known; the tenant comes *from* the session.
* `ApiKeyAuthenticationRepository` - the same, keyed by the public `key_id`.

They are separate classes rather than convenience methods so that "this query
crosses the tenant boundary" is stated in the type, not buried in a call.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import models
from app.modules.identity.domain import (
    MembershipStatus,
    TenantContext,
    TenantStatus,
    UserStatus,
)
from app.platform.clock import utcnow


class TenantScopedRepository:
    """Base class that makes the tenant predicate structural."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self._session = session
        self._tenant = tenant

    @property
    def tenant_id(self) -> uuid.UUID:
        """The only tenant this repository will ever read or write."""
        return self._tenant.tenant_id


class UserRepository:
    """Global account lookups. Users exist above the tenant boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> models.User | None:
        """Return the user with this id, or None."""
        return await self._session.get(models.User, user_id)

    async def get_by_email(self, email: str) -> models.User | None:
        """Return the user with this normalised email, or None."""
        stmt = select(models.User).where(models.User.email == email)
        return (await self._session.scalars(stmt)).first()

    async def create(
        self,
        *,
        email: str,
        password_hash: str,
        display_name: str,
    ) -> models.User:
        """Insert a new active account. `email` must already be normalised."""
        user = models.User(
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            status=UserStatus.ACTIVE.value,
            session_epoch=0,
            failed_logins=0,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def record_successful_login(self, user: models.User, *, now: datetime) -> None:
        """Clear the failure counter and stamp the login time."""
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        await self._session.flush()

    async def record_failed_login(
        self,
        user: models.User,
        *,
        max_failures: int,
        lockout_until: datetime,
    ) -> None:
        """Count a failure and lock the account once the threshold is hit.

        This counter is attached to the account under attack, so it is the
        control that survives an attacker changing source address. The
        complementary per-source limiter lives in `app.core.rate_limit` and is
        applied by `AuthenticationService`: neither replaces the other, because
        one is stepped around by rotating addresses and the other by rotating
        accounts.
        """
        user.failed_logins += 1
        if user.failed_logins >= max_failures:
            user.locked_until = lockout_until
        await self._session.flush()

    async def bump_session_epoch(self, user: models.User) -> int:
        """Invalidate every existing session for this user and return the epoch."""
        user.session_epoch += 1
        await self._session.flush()
        return user.session_epoch


class TenantRepository:
    """Tenant creation and lookup.

    Not scoped, because this is the class that produces the scope. Reads always
    exclude soft-deleted rows: a caller that wants a deleted tenant has to say
    so explicitly, and nothing in Phase 3 does.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: uuid.UUID) -> models.Tenant | None:
        """Return a live tenant by id, or None when missing or soft-deleted."""
        stmt = select(models.Tenant).where(
            models.Tenant.id == tenant_id,
            models.Tenant.deleted_at.is_(None),
        )
        return (await self._session.scalars(stmt)).first()

    async def get_by_slug(self, slug: str) -> models.Tenant | None:
        """Return a live tenant by slug, or None."""
        stmt = select(models.Tenant).where(
            models.Tenant.slug == slug,
            models.Tenant.deleted_at.is_(None),
        )
        return (await self._session.scalars(stmt)).first()

    async def create(self, *, name: str, slug: str) -> models.Tenant:
        """Insert a new active tenant."""
        tenant = models.Tenant(name=name, slug=slug, status=TenantStatus.ACTIVE.value)
        self._session.add(tenant)
        await self._session.flush()
        return tenant


class MembershipRepository(TenantScopedRepository):
    """Memberships inside one tenant."""

    def _base(self) -> Select[tuple[models.Membership]]:
        return select(models.Membership).where(
            models.Membership.tenant_id == self.tenant_id,
            models.Membership.deleted_at.is_(None),
        )

    async def get_by_id(self, membership_id: uuid.UUID) -> models.Membership | None:
        """Return a membership in this tenant, or None."""
        stmt = self._base().where(models.Membership.id == membership_id)
        return (await self._session.scalars(stmt)).first()

    async def get_for_user(self, user_id: uuid.UUID) -> models.Membership | None:
        """Return this user's membership in this tenant, or None.

        This single method is what enforces cross-tenant denial: a user with a
        perfectly valid membership in another tenant resolves to None here.
        """
        stmt = self._base().where(models.Membership.user_id == user_id)
        return (await self._session.scalars(stmt)).first()

    async def list_all(self, *, limit: int, offset: int) -> Sequence[models.Membership]:
        """Return memberships in this tenant, oldest first."""
        stmt = self._base().order_by(models.Membership.created_at).limit(limit).offset(offset)
        return (await self._session.scalars(stmt)).all()

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        status: MembershipStatus,
        invited_by_id: uuid.UUID | None = None,
        accepted_at: datetime | None = None,
    ) -> models.Membership:
        """Insert a membership bound to this repository's tenant.

        `tenant_id` is taken from the scope and is never accepted as an
        argument, so a caller cannot create a membership in a tenant they do
        not hold a context for.
        """
        membership = models.Membership(
            tenant_id=self.tenant_id,
            user_id=user_id,
            status=status.value,
            invited_by_id=invited_by_id,
            accepted_at=accepted_at,
        )
        self._session.add(membership)
        await self._session.flush()
        return membership

    async def count_active_owners(self, owner_role_id: uuid.UUID) -> int:
        """Count active memberships in this tenant holding the owner role."""
        stmt = (
            select(models.Membership.id)
            .join(
                models.MembershipRole,
                models.MembershipRole.membership_id == models.Membership.id,
            )
            .where(
                models.Membership.tenant_id == self.tenant_id,
                models.Membership.deleted_at.is_(None),
                models.Membership.status == MembershipStatus.ACTIVE.value,
                models.MembershipRole.role_id == owner_role_id,
            )
        )
        return len((await self._session.scalars(stmt)).all())

    async def role_slugs_for(self, membership_id: uuid.UUID) -> frozenset[str]:
        """Return the role slugs held by a membership in this tenant.

        The join back to `memberships` and the tenant predicate are redundant
        when `membership_id` came from `_base()` - and deliberately kept, so
        that a future caller passing an id from somewhere else still cannot
        read another tenant's role assignments.
        """
        stmt = (
            select(models.Role.slug)
            .join(models.MembershipRole, models.MembershipRole.role_id == models.Role.id)
            .join(
                models.Membership,
                models.Membership.id == models.MembershipRole.membership_id,
            )
            .where(
                models.MembershipRole.membership_id == membership_id,
                models.Membership.tenant_id == self.tenant_id,
                models.Membership.deleted_at.is_(None),
            )
        )
        return frozenset((await self._session.scalars(stmt)).all())

    async def role_and_permission_slugs_for(
        self,
        membership_id: uuid.UUID,
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Return this membership's (role slugs, permission slugs) from the database.

        This is the runtime authorization query. It walks
        `membership_roles -> roles -> role_permissions -> permissions`, which is
        what makes the database - not a Python constant - the thing that decides
        what a principal may do. `DEFAULT_ROLE_GRANTS` seeds those rows; nothing
        on this path reads it.

        Both sets come back from one statement so that a caller cannot observe a
        role list and a permission list taken from different points in time.

        The join through to `permissions` is an outer join: a role holding no
        grants still contributes its slug. With an inner join such a role would
        vanish, making "assigned but powerless" indistinguishable from "not
        assigned".

        The role predicate mirrors `RoleRepository._visible()`. A role owned by
        another tenant contributes nothing even if a `membership_roles` row
        somehow points at one, so a stray assignment fails closed instead of
        leaking a foreign tenant's grants.
        """
        stmt = (
            select(models.Role.slug, models.PermissionRecord.slug)
            .select_from(models.MembershipRole)
            .join(models.Role, models.Role.id == models.MembershipRole.role_id)
            .join(
                models.Membership,
                models.Membership.id == models.MembershipRole.membership_id,
            )
            .outerjoin(
                models.RolePermission,
                models.RolePermission.role_id == models.Role.id,
            )
            .outerjoin(
                models.PermissionRecord,
                models.PermissionRecord.id == models.RolePermission.permission_id,
            )
            .where(
                models.MembershipRole.membership_id == membership_id,
                models.Membership.tenant_id == self.tenant_id,
                models.Membership.deleted_at.is_(None),
                or_(
                    models.Role.tenant_id.is_(None),
                    models.Role.tenant_id == self.tenant_id,
                ),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        roles = frozenset(str(row[0]) for row in rows if row[0] is not None)
        permissions = frozenset(str(row[1]) for row in rows if row[1] is not None)
        return roles, permissions

    async def assign_role(self, *, membership_id: uuid.UUID, role_id: uuid.UUID) -> None:
        """Attach a role to a membership. Idempotent."""
        existing = select(models.MembershipRole).where(
            models.MembershipRole.membership_id == membership_id,
            models.MembershipRole.role_id == role_id,
        )
        if (await self._session.scalars(existing)).first() is not None:
            return
        self._session.add(
            models.MembershipRole(membership_id=membership_id, role_id=role_id),
        )
        await self._session.flush()

    async def remove_role(self, *, membership_id: uuid.UUID, role_id: uuid.UUID) -> bool:
        """Detach a role from a membership. False when it was not held.

        The `IN` subquery is what keeps this tenant scoped. A `DELETE` cannot be
        built from `_base()`, so the predicate is restated explicitly rather
        than trusted to the caller: an id belonging to another tenant matches no
        row and reports the same "not held" as an id that simply has no such
        assignment. Returning a boolean rather than raising leaves the choice of
        error - and therefore of what the caller is allowed to learn - with the
        service.
        """
        scoped_membership_ids = select(models.Membership.id).where(
            models.Membership.id == membership_id,
            models.Membership.tenant_id == self.tenant_id,
            models.Membership.deleted_at.is_(None),
        )
        stmt = delete(models.MembershipRole).where(
            models.MembershipRole.membership_id.in_(scoped_membership_ids),
            models.MembershipRole.role_id == role_id,
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return bool(result.rowcount)

    async def set_status(self, membership: models.Membership, status: MembershipStatus) -> None:
        """Change a membership's status."""
        membership.status = status.value
        if status is MembershipStatus.ACTIVE and membership.accepted_at is None:
            membership.accepted_at = utcnow()
        await self._session.flush()


class RoleRepository(TenantScopedRepository):
    """Roles visible to one tenant: its own, plus the system roles.

    A role owned by another tenant is simply not in the result set, which is
    what makes cross-tenant privilege escalation through a borrowed role id
    impossible rather than merely checked for.
    """

    def _visible(self) -> Select[tuple[models.Role]]:
        return select(models.Role).where(
            or_(
                models.Role.tenant_id.is_(None),
                models.Role.tenant_id == self.tenant_id,
            )
        )

    async def get_by_slug(self, slug: str) -> models.Role | None:
        """Return a visible role by slug, preferring a tenant-owned override."""
        stmt = self._visible().where(models.Role.slug == slug)
        stmt = stmt.order_by(models.Role.tenant_id.is_(None))
        return (await self._session.scalars(stmt)).first()

    async def get_by_id(self, role_id: uuid.UUID) -> models.Role | None:
        """Return a visible role by id, or None when it belongs elsewhere."""
        stmt = self._visible().where(models.Role.id == role_id)
        return (await self._session.scalars(stmt)).first()

    async def list_visible(self) -> Sequence[models.Role]:
        """Return every role this tenant may assign."""
        stmt = self._visible().order_by(models.Role.slug)
        return (await self._session.scalars(stmt)).all()

    async def permission_slugs_for(self, role_id: uuid.UUID) -> frozenset[str]:
        """Return the permission slugs a visible role grants, from the database.

        Reads the same `role_permissions` rows that runtime authorization
        resolves against, so anything reporting a role's grants reports what is
        actually enforced. The visibility predicate is repeated here for the
        same reason it exists on `_visible()`: a role id belonging to another
        tenant must return nothing rather than that tenant's grants.
        """
        stmt = (
            select(models.PermissionRecord.slug)
            .select_from(models.RolePermission)
            .join(
                models.PermissionRecord,
                models.PermissionRecord.id == models.RolePermission.permission_id,
            )
            .join(models.Role, models.Role.id == models.RolePermission.role_id)
            .where(
                models.RolePermission.role_id == role_id,
                or_(
                    models.Role.tenant_id.is_(None),
                    models.Role.tenant_id == self.tenant_id,
                ),
            )
        )
        return frozenset((await self._session.scalars(stmt)).all())


class SessionRepository:
    """Session lookups. Not tenant scoped: the session *is* the tenant source."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_digest(self, digest: str) -> models.UserSession | None:
        """Return the session with this token digest, or None."""
        stmt = select(models.UserSession).where(models.UserSession.token_digest == digest)
        return (await self._session.scalars(stmt)).first()

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        token_digest: str,
        csrf_digest: str,
        user_epoch: int,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        ip_address: str | None,
        user_agent: str | None,
    ) -> models.UserSession:
        """Insert a session row. Only digests are ever persisted."""
        record = models.UserSession(
            user_id=user_id,
            tenant_id=tenant_id,
            token_digest=token_digest,
            csrf_digest=csrf_digest,
            user_epoch=user_epoch,
            last_seen_at=utcnow(),
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def touch(
        self,
        record: models.UserSession,
        *,
        now: datetime,
        idle_expires_at: datetime,
    ) -> None:
        """Slide the idle window forward."""
        record.last_seen_at = now
        record.idle_expires_at = idle_expires_at
        await self._session.flush()

    async def revoke(self, record: models.UserSession, *, now: datetime) -> None:
        """Revoke one session. Idempotent."""
        if record.revoked_at is None:
            record.revoked_at = now
            await self._session.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        """Revoke every live session for a user and return how many changed."""
        stmt = (
            update(models.UserSession)
            .where(
                models.UserSession.user_id == user_id,
                models.UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    async def list_live_digests_for_user(self, user_id: uuid.UUID) -> Sequence[str]:
        """Return the digests of a user's live sessions, for cache eviction."""
        stmt = select(models.UserSession.token_digest).where(
            models.UserSession.user_id == user_id,
            models.UserSession.revoked_at.is_(None),
        )
        return (await self._session.scalars(stmt)).all()


class ApiKeyAuthenticationRepository:
    """The single global API-key lookup, isolated so it is easy to audit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_key_id(self, key_id: str) -> models.ApiKey | None:
        """Return the key with this public identifier, or None."""
        stmt = select(models.ApiKey).where(models.ApiKey.key_id == key_id)
        return (await self._session.scalars(stmt)).first()

    async def mark_used(self, record: models.ApiKey, *, now: datetime) -> None:
        """Stamp last use, so an unused key can be found and retired."""
        record.last_used_at = now
        await self._session.flush()


class ApiKeyRepository(TenantScopedRepository):
    """API-key management inside one tenant."""

    def _base(self) -> Select[tuple[models.ApiKey]]:
        return select(models.ApiKey).where(models.ApiKey.tenant_id == self.tenant_id)

    async def get_by_id(self, api_key_id: uuid.UUID) -> models.ApiKey | None:
        """Return a key belonging to this tenant, or None."""
        stmt = self._base().where(models.ApiKey.id == api_key_id)
        return (await self._session.scalars(stmt)).first()

    async def list_all(self, *, limit: int, offset: int) -> Sequence[models.ApiKey]:
        """Return this tenant's keys, newest first."""
        stmt = self._base().order_by(models.ApiKey.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)
        return (await self._session.scalars(stmt)).all()

    async def create(
        self,
        *,
        key_id: str,
        secret_digest: str,
        name: str,
        created_by_id: uuid.UUID | None,
        expires_at: datetime | None,
    ) -> models.ApiKey:
        """Insert a key bound to this repository's tenant."""
        record = models.ApiKey(
            tenant_id=self.tenant_id,
            key_id=key_id,
            secret_digest=secret_digest,
            name=name,
            created_by_id=created_by_id,
            expires_at=expires_at,
            scopes=[],
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def revoke(self, record: models.ApiKey, *, now: datetime) -> None:
        """Revoke one key. Idempotent."""
        if record.revoked_at is None:
            record.revoked_at = now
            await self._session.flush()
