"""Proof that runtime authorization is resolved from PostgreSQL.

Phase 3 originally expanded a membership's role slugs through the Python
constant `DEFAULT_ROLE_GRANTS`. A grant edited in `role_permissions` changed
nothing at all, which made the database's RBAC tables decorative. These tests
exist so that regression cannot be reintroduced quietly.

The shape of every test here is the same, and it is deliberate: mutate
`role_permissions`, leave `DEFAULT_ROLE_GRANTS` alone, then assert that the
authorization answer followed the database *and* that the Python matrix still
disagrees. That second assertion is what gives the test its teeth. If someone
reinstates the constant on the authorization path, the database and the
constant will disagree, the constant will win, and these tests fail.

Real PostgreSQL is required: the mutations are SQL against seeded reference
data, and the seeded rows come from migration 0002. The caching tests
additionally require real Redis and skip without it, matching the existing
opt-in integration fixtures.

Each test runs inside a transaction that is rolled back, so mutating a shared
system role's grants is safe - the change is never visible to another test or
left behind in the database.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import PasswordHashingService
from app.core.settings import AuthSettings
from app.modules.identity import models
from app.modules.identity.domain import (
    DEFAULT_ROLE_GRANTS,
    MembershipStatus,
    Permission,
    Principal,
    PrincipalKind,
    RoleSlug,
    TenantContext,
)
from app.modules.identity.errors import PermissionDeniedError
from app.modules.identity.repositories import (
    MembershipRepository,
    RoleRepository,
    TenantRepository,
    UserRepository,
)
from app.modules.identity.services.authorization import require_permission
from app.modules.identity.services.permissions import (
    EffectivePermissionCache,
    PermissionResolver,
)
from app.platform.clock import utcnow

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
ENVIRONMENT = "test"


def _auth_settings(**overrides: Any) -> AuthSettings:
    """Argon2id at the lowest cost the settings permit."""
    defaults: dict[str, Any] = {
        "argon2_time_cost": 1,
        "argon2_memory_kib": 8_192,
        "argon2_parallelism": 1,
    }
    defaults.update(overrides)
    return AuthSettings(**defaults)


@pytest.fixture
async def db_session(postgres_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back."""
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
def passwords() -> PasswordHashingService:
    return PasswordHashingService(_auth_settings())


@dataclass(slots=True)
class Workspace:
    """A tenant with one member holding one or more roles."""

    tenant: models.Tenant
    user: models.User
    membership: models.Membership

    @property
    def context(self) -> TenantContext:
        return TenantContext(tenant_id=self.tenant.id)


def _unique_email() -> str:
    return f"member-{uuid.uuid4().hex}@example.com"


def _unique_slug() -> str:
    return f"acme-{uuid.uuid4().hex[:12]}"


async def _create_workspace(
    session: AsyncSession,
    passwords: PasswordHashingService,
    *,
    roles: tuple[RoleSlug, ...] = (RoleSlug.ADMIN,),
) -> Workspace:
    """Create a tenant with one active member holding `roles`."""
    user = await UserRepository(session).create(
        email=_unique_email(),
        password_hash=passwords.hash(PASSWORD),
        display_name="Test Person",
    )
    tenant = await TenantRepository(session).create(name="Acme", slug=_unique_slug())
    context = TenantContext(tenant_id=tenant.id)
    memberships = MembershipRepository(session, context)
    membership = await memberships.create(
        user_id=user.id,
        status=MembershipStatus.ACTIVE,
        accepted_at=utcnow(),
    )
    role_repository = RoleRepository(session, context)
    for slug in roles:
        record = await role_repository.get_by_slug(slug.value)
        assert record is not None, "system roles must be seeded by migration 0002"
        await memberships.assign_role(membership_id=membership.id, role_id=record.id)
    return Workspace(tenant=tenant, user=user, membership=membership)


def _uncached_resolver(session: AsyncSession, workspace: Workspace) -> PermissionResolver:
    """A resolver that always reads PostgreSQL.

    `ttl_seconds=0` disables the cache outright, so these tests observe the
    database directly and a stale entry can never be mistaken for a correct
    answer.
    """
    memberships = MembershipRepository(session, workspace.context)
    cache = EffectivePermissionCache(None, environment=ENVIRONMENT, ttl_seconds=0)
    return PermissionResolver(memberships, cache)


def _cached_resolver(
    session: AsyncSession,
    workspace: Workspace,
    redis: Redis,
    *,
    ttl_seconds: int = 60,
) -> PermissionResolver:
    """A resolver backed by real Redis."""
    memberships = MembershipRepository(session, workspace.context)
    cache = EffectivePermissionCache(
        redis,
        environment=ENVIRONMENT,
        ttl_seconds=ttl_seconds,
    )
    return PermissionResolver(memberships, cache)


async def _revoke_grant(
    session: AsyncSession,
    role: RoleSlug,
    permission: Permission,
) -> None:
    """Delete one role -> permission row from the system role's grants."""
    result: CursorResult[Any] = await session.execute(
        text(
            "DELETE FROM role_permissions "
            "WHERE role_id = ("
            "  SELECT id FROM roles WHERE slug = :role AND tenant_id IS NULL"
            ") AND permission_id = ("
            "  SELECT id FROM permissions WHERE slug = :permission"
            ")"
        ),
        {"role": role.value, "permission": permission.value},
    )
    assert result.rowcount == 1, "expected exactly one seeded grant to be removed"


async def _add_grant(session: AsyncSession, role: RoleSlug, permission: Permission) -> None:
    """Insert one role -> permission row into the system role's grants."""
    await session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.slug = :role AND r.tenant_id IS NULL AND p.slug = :permission"
        ),
        {"role": role.value, "permission": permission.value},
    )


# --------------------------------------------------------------------------
# 1. role_permissions controls authorization
# --------------------------------------------------------------------------


async def test_removing_a_database_grant_revokes_the_permission(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """The load-bearing test. Admin loses conversations.reply from the database.

    `DEFAULT_ROLE_GRANTS` is not touched and is asserted to still contain the
    permission. An implementation that consulted the Python matrix would answer
    "allowed" here and fail.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.ADMIN,))
    resolver = _uncached_resolver(db_session, workspace)

    granted = await resolver.permissions_for(workspace.membership.id)
    assert Permission.CONVERSATIONS_REPLY in granted

    await _revoke_grant(db_session, RoleSlug.ADMIN, Permission.CONVERSATIONS_REPLY)

    revoked = await resolver.permissions_for(workspace.membership.id)
    assert Permission.CONVERSATIONS_REPLY not in revoked
    # The Python matrix still says otherwise. That disagreement is the proof.
    assert Permission.CONVERSATIONS_REPLY in DEFAULT_ROLE_GRANTS[RoleSlug.ADMIN]
    # Everything else the role holds is untouched: this removed one grant, not
    # the role's authority in general.
    assert Permission.CONVERSATIONS_READ in revoked


async def test_a_revoked_grant_produces_the_existing_403(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """Fail-closed behaviour is unchanged: the caller still gets 403, not 500."""
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.ADMIN,))
    resolver = _uncached_resolver(db_session, workspace)
    await _revoke_grant(db_session, RoleSlug.ADMIN, Permission.CONVERSATIONS_REPLY)

    principal = Principal(
        kind=PrincipalKind.USER,
        tenant_id=workspace.tenant.id,
        permissions=await resolver.permissions_for(workspace.membership.id),
        role_slugs=await resolver.role_slugs_for(workspace.membership.id),
        user_id=workspace.user.id,
        membership_id=workspace.membership.id,
    )
    with pytest.raises(PermissionDeniedError) as caught:
        require_permission(principal, Permission.CONVERSATIONS_REPLY)
    assert caught.value.http_status == 403


# --------------------------------------------------------------------------
# 2. a database grant confers a permission the Python matrix does not
# --------------------------------------------------------------------------


async def test_adding_a_database_grant_confers_the_permission(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """Viewer gains billing.manage from the database alone.

    `DEFAULT_ROLE_GRANTS[VIEWER]` does not contain it and is not modified, so
    an implementation reading the Python matrix would answer "denied".
    """
    assert Permission.BILLING_MANAGE not in DEFAULT_ROLE_GRANTS[RoleSlug.VIEWER]

    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))
    resolver = _uncached_resolver(db_session, workspace)

    before = await resolver.permissions_for(workspace.membership.id)
    assert Permission.BILLING_MANAGE not in before

    await _add_grant(db_session, RoleSlug.VIEWER, Permission.BILLING_MANAGE)

    after = await resolver.permissions_for(workspace.membership.id)
    assert Permission.BILLING_MANAGE in after
    assert Permission.BILLING_MANAGE not in DEFAULT_ROLE_GRANTS[RoleSlug.VIEWER]


# --------------------------------------------------------------------------
# 3. caching and invalidation
# --------------------------------------------------------------------------


async def test_a_resolution_is_cached_and_invalidation_reveals_the_change(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
    redis_client: Redis,
) -> None:
    """Three facts in one test, because they only mean anything together.

    That the value is cached, that a database change is therefore *not* seen
    immediately, and that invalidation makes it visible. Asserting the stale
    read in the middle is what proves the cache is actually being consulted
    rather than quietly bypassed.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.ADMIN,))
    resolver = _cached_resolver(db_session, workspace, redis_client)

    try:
        first = await resolver.permissions_for(workspace.membership.id)
        assert Permission.CONVERSATIONS_REPLY in first

        await _revoke_grant(db_session, RoleSlug.ADMIN, Permission.CONVERSATIONS_REPLY)

        # Still cached: the database moved, the cache has not been told.
        stale = await resolver.permissions_for(workspace.membership.id)
        assert Permission.CONVERSATIONS_REPLY in stale

        await resolver.invalidate(workspace.membership.id)

        fresh = await resolver.permissions_for(workspace.membership.id)
        assert Permission.CONVERSATIONS_REPLY not in fresh
    finally:
        await resolver.invalidate(workspace.membership.id)


async def test_the_cached_value_is_the_effective_permission_set(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
    redis_client: Redis,
) -> None:
    """What lands in Redis must be permissions, not role slugs.

    Caching slugs is what allowed the original defect to hide: a slug still has
    to be expanded by something, and that something was the Python matrix.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.AGENT,))
    resolver = _cached_resolver(db_session, workspace, redis_client)

    try:
        await resolver.permissions_for(workspace.membership.id)
        key = (
            f"oc:{ENVIRONMENT}:t:{workspace.tenant.id}:rbac-perms:{workspace.membership.id}"
        )
        stored = await redis_client.get(key)
        assert stored is not None, "the resolution should have been cached"
        assert Permission.CONVERSATIONS_READ.value in stored
        assert "agent" in stored
    finally:
        await resolver.invalidate(workspace.membership.id)


# --------------------------------------------------------------------------
# 4. tenant isolation
# --------------------------------------------------------------------------


async def test_one_tenants_cached_permissions_cannot_reach_another(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
    redis_client: Redis,
) -> None:
    """Tenant A's cache entry must not be readable or usable as tenant B's.

    The tenant id is a structural part of the key rather than a field inside
    the value, so this is a property of the key layout, not of a comparison
    somebody has to remember to write.
    """
    first = await _create_workspace(db_session, passwords, roles=(RoleSlug.OWNER,))
    second = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))

    first_resolver = _cached_resolver(db_session, first, redis_client)
    second_resolver = _cached_resolver(db_session, second, redis_client)

    try:
        owner_permissions = await first_resolver.permissions_for(first.membership.id)
        viewer_permissions = await second_resolver.permissions_for(second.membership.id)

        assert Permission.BILLING_MANAGE in owner_permissions
        assert Permission.BILLING_MANAGE not in viewer_permissions

        # Distinct keys, each naming its own tenant.
        first_key = f"oc:{ENVIRONMENT}:t:{first.tenant.id}:rbac-perms:{first.membership.id}"
        second_key = f"oc:{ENVIRONMENT}:t:{second.tenant.id}:rbac-perms:{second.membership.id}"
        assert first_key != second_key
        assert str(first.tenant.id) in first_key
        assert str(second.tenant.id) in second_key
        assert await redis_client.get(first_key) is not None
        assert await redis_client.get(second_key) is not None

        # Invalidating one tenant leaves the other's entry intact.
        await first_resolver.invalidate(first.membership.id)
        assert await redis_client.get(first_key) is None
        assert await redis_client.get(second_key) is not None

        # And the second tenant still resolves to its own, narrower authority.
        still_viewer = await second_resolver.permissions_for(second.membership.id)
        assert Permission.BILLING_MANAGE not in still_viewer
    finally:
        await first_resolver.invalidate(first.membership.id)
        await second_resolver.invalidate(second.membership.id)


async def test_a_membership_is_not_resolvable_through_another_tenants_scope(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """A foreign membership id resolves to no authority, not to its real one."""
    theirs = await _create_workspace(db_session, passwords, roles=(RoleSlug.OWNER,))
    ours = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))

    through_our_scope = _uncached_resolver(db_session, ours)
    leaked = await through_our_scope.permissions_for(theirs.membership.id)
    assert leaked == frozenset()


# --------------------------------------------------------------------------
# 5. multiple roles union
# --------------------------------------------------------------------------


async def test_effective_permissions_are_the_union_of_every_role(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """Two roles, and the member holds what either one grants."""
    workspace = await _create_workspace(
        db_session,
        passwords,
        roles=(RoleSlug.AGENT, RoleSlug.BILLING_ADMIN),
    )
    resolver = _uncached_resolver(db_session, workspace)

    resolved = await resolver.permissions_for(workspace.membership.id)
    assert await resolver.role_slugs_for(workspace.membership.id) == frozenset(
        {"agent", "billing_admin"}
    )
    # From agent.
    assert Permission.CONVERSATIONS_REPLY in resolved
    # From billing_admin.
    assert Permission.BILLING_MANAGE in resolved
    # From neither.
    assert Permission.APIKEYS_MANAGE not in resolved


async def test_the_union_follows_the_database_not_the_python_matrix(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """Removing a grant from one of two roles narrows the union accordingly."""
    workspace = await _create_workspace(
        db_session,
        passwords,
        roles=(RoleSlug.AGENT, RoleSlug.BILLING_ADMIN),
    )
    resolver = _uncached_resolver(db_session, workspace)
    assert Permission.BILLING_MANAGE in await resolver.permissions_for(workspace.membership.id)

    await _revoke_grant(db_session, RoleSlug.BILLING_ADMIN, Permission.BILLING_MANAGE)

    narrowed = await resolver.permissions_for(workspace.membership.id)
    assert Permission.BILLING_MANAGE not in narrowed
    # Agent's own grants are unaffected.
    assert Permission.CONVERSATIONS_REPLY in narrowed
    assert Permission.BILLING_MANAGE in DEFAULT_ROLE_GRANTS[RoleSlug.BILLING_ADMIN]


# --------------------------------------------------------------------------
# 6. fail closed
# --------------------------------------------------------------------------


async def test_a_membership_with_no_roles_holds_nothing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """No roles means no authority, and no error."""
    workspace = await _create_workspace(db_session, passwords, roles=())
    resolver = _uncached_resolver(db_session, workspace)

    assert await resolver.role_slugs_for(workspace.membership.id) == frozenset()
    assert await resolver.permissions_for(workspace.membership.id) == frozenset()


async def test_a_role_stripped_of_every_grant_holds_nothing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """A role with no rows in role_permissions confers nothing.

    The role slug is still reported - the membership does hold the role - but
    it carries no permissions. "Assigned but powerless" must be expressible.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))
    await db_session.execute(
        text(
            "DELETE FROM role_permissions WHERE role_id = ("
            "  SELECT id FROM roles WHERE slug = :role AND tenant_id IS NULL"
            ")"
        ),
        {"role": RoleSlug.VIEWER.value},
    )
    resolver = _uncached_resolver(db_session, workspace)

    assert await resolver.role_slugs_for(workspace.membership.id) == frozenset({"viewer"})
    assert await resolver.permissions_for(workspace.membership.id) == frozenset()


async def test_an_unrecognised_permission_slug_grants_nothing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    """A permission row outside the catalogue is dropped, not honoured.

    A slug the application does not know about cannot be mapped to any check,
    so admitting it would be meaningless at best. It must also not raise:
    turning every request from an affected member into a 500 would be a worse
    outage than the missing grant.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))
    await db_session.execute(
        text("INSERT INTO permissions (id, slug, description) VALUES (:id, :slug, '')"),
        {"id": uuid.uuid4(), "slug": "universe.destroy"},
    )
    await db_session.execute(
        text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.slug = :role AND r.tenant_id IS NULL AND p.slug = :slug"
        ),
        {"role": RoleSlug.VIEWER.value, "slug": "universe.destroy"},
    )
    resolver = _uncached_resolver(db_session, workspace)

    resolved = await resolver.permissions_for(workspace.membership.id)
    assert all(isinstance(permission, Permission) for permission in resolved)
    assert "universe.destroy" not in {permission.value for permission in resolved}
    # The role's real grants still work.
    assert Permission.CONVERSATIONS_READ in resolved


async def test_a_corrupt_cache_entry_is_ignored_rather_than_trusted(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
    redis_client: Redis,
) -> None:
    """An unparseable entry must degrade to a database read.

    The cache is the one place where a wrong answer could be manufactured
    without touching the database, so a value that does not parse has to be
    treated as absent.
    """
    workspace = await _create_workspace(db_session, passwords, roles=(RoleSlug.VIEWER,))
    resolver = _cached_resolver(db_session, workspace, redis_client)
    key = f"oc:{ENVIRONMENT}:t:{workspace.tenant.id}:rbac-perms:{workspace.membership.id}"

    try:
        await redis_client.setex(key, 60, "not json at all")
        resolved = await resolver.permissions_for(workspace.membership.id)
        assert Permission.CONVERSATIONS_READ in resolved
        assert Permission.BILLING_MANAGE not in resolved
    finally:
        await resolver.invalidate(workspace.membership.id)
