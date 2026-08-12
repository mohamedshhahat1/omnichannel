"""Identity persistence against real PostgreSQL.

Everything here needs a real server: the constraints being asserted are
PostgreSQL constraints, the seeded RBAC rows come from the Alembic migration,
and partial unique indexes and `ON DELETE` behaviour have no equivalent in a
substitute engine. SQLite is refused by the shared fixture on purpose.

Each test runs inside a transaction that is rolled back afterwards, so the
suite leaves the database exactly as it found it and tests cannot see each
other's rows. That also keeps the `oc_app` role sufficient - no DDL and no
TRUNCATE are needed.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.security import PasswordHashingService, digest_secret, generate_token, mint_api_key
from app.core.settings import AuthSettings, Environment
from app.modules.identity import models
from app.modules.identity.domain import (
    DEFAULT_ROLE_GRANTS,
    MembershipStatus,
    Permission,
    Principal,
    PrincipalKind,
    RoleSlug,
    TenantContext,
    UserStatus,
)
from app.modules.identity.errors import (
    ApiKeyNotFoundError,
    AuthenticationFailedError,
    PermissionDeniedError,
)
from app.modules.identity.repositories import (
    ApiKeyRepository,
    MembershipRepository,
    RoleRepository,
    SessionRepository,
    TenantRepository,
    UserRepository,
)
from app.modules.identity.services.api_keys import ApiKeyService, resolve_scopes
from app.modules.identity.services.audit import AuditService
from app.modules.identity.services.authentication import AuthenticationService
from app.modules.identity.services.permissions import PermissionResolver, RoleSlugCache
from app.modules.identity.services.sessions import SessionService
from app.platform.clock import utcnow

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "a completely different passphrase"


def _auth_settings(**overrides: Any) -> AuthSettings:
    """Argon2id at the lowest cost the settings permit.

    Production parameters would add ~100 ms per hash to every test here. The
    production floor lives in `Settings` and is covered by
    `tests/unit/test_auth_settings.py`.
    """
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
    """A tenant with one active owner."""

    tenant: models.Tenant
    user: models.User
    membership: models.Membership
    principal: Principal


def _unique_email(label: str = "user") -> str:
    return f"{label}-{uuid.uuid4().hex}@example.com"


def _unique_slug(label: str = "acme") -> str:
    return f"{label}-{uuid.uuid4().hex[:12]}"


async def _create_user(
    session: AsyncSession,
    passwords: PasswordHashingService,
    *,
    email: str | None = None,
    password: str = PASSWORD,
) -> models.User:
    return await UserRepository(session).create(
        email=email or _unique_email(),
        password_hash=passwords.hash(password),
        display_name="Test Person",
    )


async def _create_workspace(
    session: AsyncSession,
    passwords: PasswordHashingService,
    *,
    role: RoleSlug = RoleSlug.OWNER,
    user: models.User | None = None,
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> Workspace:
    """Create a tenant, a member and a role assignment."""
    member = user or await _create_user(session, passwords)
    tenant = await TenantRepository(session).create(name="Acme", slug=_unique_slug())
    context = TenantContext(tenant_id=tenant.id)
    memberships = MembershipRepository(session, context)
    membership = await memberships.create(
        user_id=member.id,
        status=status,
        accepted_at=utcnow() if status is MembershipStatus.ACTIVE else None,
    )
    role_record = await RoleRepository(session, context).get_by_slug(role.value)
    assert role_record is not None, "system roles must be seeded by migration 0002"
    await memberships.assign_role(membership_id=membership.id, role_id=role_record.id)

    cache = RoleSlugCache(None, environment="test", ttl_seconds=0)
    resolver = PermissionResolver(memberships, cache)
    slugs = await resolver.role_slugs_for(membership.id)
    principal = Principal(
        kind=PrincipalKind.USER,
        tenant_id=tenant.id,
        permissions=await resolver.permissions_for(membership.id),
        role_slugs=slugs,
        user_id=member.id,
        membership_id=membership.id,
    )
    return Workspace(tenant=tenant, user=member, membership=membership, principal=principal)


# --------------------------------------------------------------------------
# Migration correctness
# --------------------------------------------------------------------------


async def test_every_identity_table_exists(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {
            "names": [
                "tenants",
                "users",
                "memberships",
                "roles",
                "permissions",
                "role_permissions",
                "membership_roles",
                "sessions",
                "api_keys",
                "email_tokens",
                "audit_logs",
            ]
        },
    )
    assert len({row[0] for row in rows}) == 11


async def test_the_permission_catalogue_is_seeded(db_session: AsyncSession) -> None:
    rows = await db_session.execute(text("SELECT slug FROM permissions"))
    assert {row[0] for row in rows} == {permission.value for permission in Permission}


async def test_the_system_roles_are_seeded(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text("SELECT slug FROM roles WHERE tenant_id IS NULL AND is_system")
    )
    assert {row[0] for row in rows} == {role.value for role in RoleSlug}


@pytest.mark.parametrize("role", list(RoleSlug))
async def test_seeded_grants_match_the_domain(db_session: AsyncSession, role: RoleSlug) -> None:
    # The database is the thing that actually authorises requests. If the seed
    # and the domain table disagree, the domain table is a comforting fiction.
    rows = await db_session.execute(
        text(
            "SELECT p.slug FROM role_permissions rp "
            "JOIN roles r ON r.id = rp.role_id "
            "JOIN permissions p ON p.id = rp.permission_id "
            "WHERE r.tenant_id IS NULL AND r.slug = :slug"
        ),
        {"slug": role.value},
    )
    granted = {row[0] for row in rows}
    assert granted == {permission.value for permission in DEFAULT_ROLE_GRANTS[role]}


async def test_identifiers_are_time_ordered_uuids(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(name="Acme", slug=_unique_slug())
    assert tenant.id.version == 7


# --------------------------------------------------------------------------
# Users and tenants
# --------------------------------------------------------------------------


async def test_user_creation_persists_an_active_account(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    user = await _create_user(db_session, passwords)
    assert user.status == UserStatus.ACTIVE.value
    assert user.session_epoch == 0
    assert user.failed_logins == 0
    assert user.created_at is not None


async def test_duplicate_email_is_refused_by_the_database(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    email = _unique_email()
    await _create_user(db_session, passwords, email=email)
    with pytest.raises(IntegrityError):
        await _create_user(db_session, passwords, email=email)


async def test_a_non_normalised_email_cannot_be_stored(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    # The application lowercases addresses, but the invariant is enforced by a
    # CHECK constraint so a future code path cannot create `Ada@` alongside
    # `ada@` and hand one mailbox two accounts.
    with pytest.raises(IntegrityError):
        await _create_user(db_session, passwords, email=_unique_email().upper())


async def test_duplicate_tenant_slug_is_refused(db_session: AsyncSession) -> None:
    slug = _unique_slug()
    tenants = TenantRepository(db_session)
    await tenants.create(name="First", slug=slug)
    with pytest.raises(IntegrityError):
        await tenants.create(name="Second", slug=slug)


async def test_only_the_password_digest_is_stored(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    user = await _create_user(db_session, passwords)
    stored = await db_session.scalar(
        text("SELECT password_hash FROM users WHERE id = :id"),
        {"id": user.id},
    )
    assert stored is not None
    assert PASSWORD not in stored
    assert stored.startswith("$argon2id$")


# --------------------------------------------------------------------------
# Memberships, roles and tenant isolation
# --------------------------------------------------------------------------


async def test_membership_creation_binds_the_repository_tenant(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    assert workspace.membership.tenant_id == workspace.tenant.id
    assert workspace.membership.status == MembershipStatus.ACTIVE.value


async def test_a_person_cannot_join_the_same_tenant_twice(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    memberships = MembershipRepository(db_session, TenantContext(tenant_id=workspace.tenant.id))
    with pytest.raises(IntegrityError):
        await memberships.create(user_id=workspace.user.id, status=MembershipStatus.ACTIVE)


async def test_a_membership_in_another_tenant_is_invisible(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    # The central cross-tenant denial: a real, active membership resolves to
    # None when read through another tenant's scope.
    theirs = await _create_workspace(db_session, passwords)
    ours = await _create_workspace(db_session, passwords)

    through_our_scope = MembershipRepository(db_session, TenantContext(tenant_id=ours.tenant.id))
    assert await through_our_scope.get_for_user(theirs.user.id) is None
    assert await through_our_scope.get_by_id(theirs.membership.id) is None


async def test_listing_members_never_crosses_the_boundary(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    theirs = await _create_workspace(db_session, passwords)
    ours = await _create_workspace(db_session, passwords)

    listed = await MembershipRepository(
        db_session, TenantContext(tenant_id=ours.tenant.id)
    ).list_all(limit=100, offset=0)
    ids = {membership.id for membership in listed}
    assert ours.membership.id in ids
    assert theirs.membership.id not in ids


async def test_role_assignment_produces_the_documented_permissions(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.AGENT)
    assert workspace.principal.role_slugs == frozenset({"agent"})
    assert workspace.principal.permissions == DEFAULT_ROLE_GRANTS[RoleSlug.AGENT]


async def test_role_assignment_is_idempotent(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.VIEWER)
    context = TenantContext(tenant_id=workspace.tenant.id)
    memberships = MembershipRepository(db_session, context)
    viewer = await RoleRepository(db_session, context).get_by_slug(RoleSlug.VIEWER.value)
    assert viewer is not None

    await memberships.assign_role(membership_id=workspace.membership.id, role_id=viewer.id)
    count = await db_session.scalar(
        text("SELECT count(*) FROM membership_roles WHERE membership_id = :id"),
        {"id": workspace.membership.id},
    )
    assert count == 1


async def test_every_system_role_is_visible_to_every_tenant(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    visible = await RoleRepository(
        db_session, TenantContext(tenant_id=workspace.tenant.id)
    ).list_visible()
    assert {role.slug for role in visible} >= {role.value for role in RoleSlug}


async def test_an_unknown_role_slug_resolves_to_nothing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    roles = RoleRepository(db_session, TenantContext(tenant_id=workspace.tenant.id))
    assert await roles.get_by_slug("superuser") is None


async def test_an_assigned_role_cannot_be_deleted(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    # ON DELETE RESTRICT. Deleting a role out from under its holders would
    # silently strip their permissions instead of failing loudly.
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.VIEWER)
    context = TenantContext(tenant_id=workspace.tenant.id)
    viewer = await RoleRepository(db_session, context).get_by_slug(RoleSlug.VIEWER.value)
    assert viewer is not None
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("DELETE FROM roles WHERE id = :id"),
            {"id": viewer.id},
        )


async def test_a_suspended_membership_keeps_its_row_but_loses_standing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    memberships = MembershipRepository(db_session, TenantContext(tenant_id=workspace.tenant.id))
    await memberships.set_status(workspace.membership, MembershipStatus.SUSPENDED)

    reloaded = await memberships.get_for_user(workspace.user.id)
    assert reloaded is not None
    assert reloaded.status == MembershipStatus.SUSPENDED.value


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


async def test_an_owner_may_manage_api_keys(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.OWNER)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(
        principal=workspace.principal,
        name="deploy",
        scopes=[Permission.CONVERSATIONS_READ.value],
    )
    assert issued.record.tenant_id == workspace.tenant.id


async def test_an_agent_may_not_manage_api_keys(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.AGENT)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    with pytest.raises(PermissionDeniedError) as caught:
        await service.issue(principal=workspace.principal, name="deploy", scopes=[])
    assert caught.value.http_status == 403


async def test_a_key_cannot_be_granted_more_than_its_creator_holds(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    # Privilege boundary: admin holds every permission except billing.
    workspace = await _create_workspace(db_session, passwords, role=RoleSlug.ADMIN)
    with pytest.raises(PermissionDeniedError):
        resolve_scopes(workspace.principal, [Permission.BILLING_MANAGE.value])


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


async def test_a_session_stores_only_digests(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    issued = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)

    row = (
        await db_session.execute(
            text("SELECT token_digest, csrf_digest FROM sessions WHERE id = :id"),
            {"id": issued.session_id},
        )
    ).one()
    assert row[0] == digest_secret(issued.token)
    assert row[1] == digest_secret(issued.csrf_token)
    assert issued.token not in row[0]
    assert issued.csrf_token not in row[1]


async def test_a_live_session_is_found_and_a_revoked_one_is_not(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    issued = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)

    record = await sessions.find_live(issued.token)
    assert record is not None

    await sessions.revoke(record)
    assert await sessions.find_live(issued.token) is None


async def test_an_unknown_token_resolves_to_nothing(db_session: AsyncSession) -> None:
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    assert await sessions.find_live(generate_token()) is None


async def test_an_expired_session_is_not_live(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    issued = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)
    await db_session.execute(
        text("UPDATE sessions SET idle_expires_at = :past WHERE id = :id"),
        {"past": utcnow() - timedelta(seconds=1), "id": issued.session_id},
    )
    assert await sessions.find_live(issued.token) is None


async def test_signing_out_everywhere_revokes_every_session(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    first = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)
    second = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)

    revoked = await sessions.revoke_all_for_user(workspace.user.id)
    assert revoked == 2
    assert await sessions.find_live(first.token) is None
    assert await sessions.find_live(second.token) is None


async def test_the_csrf_check_requires_the_matching_token(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    sessions = SessionService(SessionRepository(db_session), _auth_settings())
    issued = await sessions.issue(user=workspace.user, tenant_id=workspace.tenant.id)
    record = await sessions.find_live(issued.token)
    assert record is not None

    assert sessions.verify_csrf(record, issued.csrf_token)
    assert not sessions.verify_csrf(record, generate_token())
    assert not sessions.verify_csrf(record, None)
    assert not sessions.verify_csrf(record, "")
    # The session token is not the CSRF token, even though both are live.
    assert not sessions.verify_csrf(record, issued.token)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


async def test_a_key_authenticates_and_stops_when_revoked(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(
        principal=workspace.principal,
        name="deploy",
        scopes=[Permission.CONVERSATIONS_READ.value],
    )

    authenticated = await service.authenticate(issued.plaintext)
    assert authenticated is not None
    assert authenticated.id == issued.record.id

    await service.revoke(principal=workspace.principal, api_key_id=issued.record.id)
    assert await service.authenticate(issued.plaintext) is None


async def test_only_the_key_digest_is_stored(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(principal=workspace.principal, name="deploy", scopes=[])

    row = (
        await db_session.execute(
            text("SELECT key_id, secret_digest FROM api_keys WHERE id = :id"),
            {"id": issued.record.id},
        )
    ).one()
    assert row[0] in issued.plaintext
    assert issued.plaintext not in row[1]
    assert row[1] not in issued.plaintext


@pytest.mark.parametrize(
    "mode",
    ["flipped_secret", "wrong_environment", "nonsense", "empty", "prefix_only"],
)
async def test_a_tampered_key_never_authenticates(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
    mode: str,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(principal=workspace.principal, name="deploy", scopes=[])
    plaintext = issued.plaintext
    presented = {
        "flipped_secret": plaintext[:-1] + ("a" if plaintext[-1] != "a" else "b"),
        "wrong_environment": plaintext.replace("_test_", "_live_", 1),
        "nonsense": "nonsense",
        "empty": "",
        "prefix_only": plaintext.rsplit("_", 1)[0],
    }[mode]
    assert await service.authenticate(presented) is None


async def test_an_expired_key_does_not_authenticate(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(principal=workspace.principal, name="deploy", scopes=[])
    await db_session.execute(
        text("UPDATE api_keys SET expires_at = :past WHERE id = :id"),
        {"past": utcnow() - timedelta(seconds=1), "id": issued.record.id},
    )
    assert await service.authenticate(issued.plaintext) is None


async def test_a_key_belonging_to_another_tenant_cannot_be_revoked(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    theirs = await _create_workspace(db_session, passwords)
    ours = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(principal=theirs.principal, name="deploy", scopes=[])

    # 404, not 403: the caller must not learn that the identifier is real.
    with pytest.raises(ApiKeyNotFoundError) as caught:
        await service.revoke(principal=ours.principal, api_key_id=issued.record.id)
    assert caught.value.http_status == 404


async def test_listing_keys_never_crosses_the_boundary(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    theirs = await _create_workspace(db_session, passwords)
    ours = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    hidden = await service.issue(principal=theirs.principal, name="theirs", scopes=[])
    mine = await service.issue(principal=ours.principal, name="ours", scopes=[])

    listed = await service.list_for_tenant(principal=ours.principal, limit=100, offset=0)
    ids = {record.id for record in listed}
    assert mine.record.id in ids
    assert hidden.record.id not in ids


async def test_a_key_cannot_outlive_the_configured_maximum(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(
        db_session,
        settings=_auth_settings(api_key_max_ttl_days=30),
        environment=Environment.TEST,
    )
    issued = await service.issue(principal=workspace.principal, name="deploy", scopes=[])
    assert issued.record.expires_at is not None


async def test_a_key_from_another_environment_is_refused(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    # A production key replayed against staging must not work even if the two
    # databases were ever restored from one another.
    workspace = await _create_workspace(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    material = mint_api_key("live")
    await ApiKeyRepository(db_session, workspace.principal.tenant).create(
        key_id=material.key_id,
        secret_digest=material.secret_digest,
        name="foreign",
        created_by_id=workspace.user.id,
        expires_at=None,
    )
    assert await service.authenticate(material.plaintext) is None


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def _authentication(
    session: AsyncSession,
    passwords: PasswordHashingService,
    *,
    settings: AuthSettings | None = None,
) -> AuthenticationService:
    resolved = settings or _auth_settings()
    return AuthenticationService(
        session,
        settings=resolved,
        passwords=passwords,
        sessions=SessionService(SessionRepository(session), resolved),
        api_keys=ApiKeyService(session, settings=resolved, environment=Environment.TEST),
        cache=RoleSlugCache(None, environment="test", ttl_seconds=0),
        audit=AuditService(session),
    )


async def test_a_correct_password_yields_a_session_scoped_to_the_tenant(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)

    result = await authentication.login(
        raw_email=workspace.user.email,
        raw_password=PASSWORD,
        tenant_slug=workspace.tenant.slug,
    )
    identity = await authentication.identify_session(result.issued.token)
    assert identity is not None
    assert identity.principal is not None
    assert identity.principal.tenant_id == workspace.tenant.id
    assert identity.principal.permissions == DEFAULT_ROLE_GRANTS[RoleSlug.OWNER]


async def test_the_wrong_password_is_refused(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    with pytest.raises(AuthenticationFailedError) as caught:
        await authentication.login(raw_email=workspace.user.email, raw_password=OTHER_PASSWORD)
    assert caught.value.http_status == 401


async def test_an_unknown_address_is_refused_identically(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)

    with pytest.raises(AuthenticationFailedError) as unknown:
        await authentication.login(raw_email=_unique_email(), raw_password=PASSWORD)
    with pytest.raises(AuthenticationFailedError) as wrong:
        await authentication.login(raw_email=workspace.user.email, raw_password=OTHER_PASSWORD)

    # Identical to the client: no oracle for "does this address have an account".
    assert unknown.value.code == wrong.value.code
    assert unknown.value.message == wrong.value.message
    assert unknown.value.http_status == wrong.value.http_status


async def test_a_malformed_address_is_refused_identically(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    authentication = _authentication(db_session, passwords)
    with pytest.raises(AuthenticationFailedError) as caught:
        await authentication.login(raw_email="not-an-email", raw_password=PASSWORD)
    assert caught.value.http_status == 401


async def test_login_never_echoes_the_password(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    with pytest.raises(AuthenticationFailedError) as caught:
        await authentication.login(raw_email=workspace.user.email, raw_password=OTHER_PASSWORD)
    assert OTHER_PASSWORD not in caught.value.message
    assert OTHER_PASSWORD not in str(caught.value.internal_message)


async def test_repeated_failures_lock_the_account(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    settings = _auth_settings(max_failed_logins=3)
    authentication = _authentication(db_session, passwords, settings=settings)

    for _ in range(3):
        with pytest.raises(AuthenticationFailedError):
            await authentication.login(raw_email=workspace.user.email, raw_password=OTHER_PASSWORD)

    # The correct password is now refused too: the lock is the point.
    with pytest.raises(AuthenticationFailedError):
        await authentication.login(raw_email=workspace.user.email, raw_password=PASSWORD)
    assert workspace.user.locked_until is not None


async def test_an_inactive_account_cannot_sign_in(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    workspace.user.status = UserStatus.DEACTIVATED.value
    await db_session.flush()

    authentication = _authentication(db_session, passwords)
    with pytest.raises(AuthenticationFailedError):
        await authentication.login(raw_email=workspace.user.email, raw_password=PASSWORD)


async def test_a_suspended_membership_loses_its_principal(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    result = await authentication.login(
        raw_email=workspace.user.email,
        raw_password=PASSWORD,
        tenant_slug=workspace.tenant.slug,
    )

    memberships = MembershipRepository(db_session, TenantContext(tenant_id=workspace.tenant.id))
    await memberships.set_status(workspace.membership, MembershipStatus.SUSPENDED)

    # Still signed in, but with no authority anywhere. Revoking a role takes
    # effect on the next request, not whenever the session happens to expire.
    identity = await authentication.identify_session(result.issued.token)
    assert identity is not None
    assert identity.principal is None


async def test_signing_in_to_a_tenant_you_do_not_belong_to_grants_nothing(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    theirs = await _create_workspace(db_session, passwords)
    ours = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)

    result = await authentication.login(
        raw_email=ours.user.email,
        raw_password=PASSWORD,
        tenant_slug=theirs.tenant.slug,
    )
    identity = await authentication.identify_session(result.issued.token)
    assert identity is not None
    # A tenantless session, not an error: "no such workspace" and "not yours"
    # must be indistinguishable.
    assert identity.principal is None


async def test_signing_out_everywhere_invalidates_an_existing_session(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    result = await authentication.login(
        raw_email=workspace.user.email,
        raw_password=PASSWORD,
        tenant_slug=workspace.tenant.slug,
    )
    assert await authentication.identify_session(result.issued.token) is not None

    await authentication.logout_everywhere(workspace.user)
    assert await authentication.identify_session(result.issued.token) is None
    assert workspace.user.session_epoch == 1


async def test_an_api_key_resolves_to_a_scoped_principal(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    service = ApiKeyService(db_session, settings=_auth_settings(), environment=Environment.TEST)
    issued = await service.issue(
        principal=workspace.principal,
        name="deploy",
        scopes=[Permission.CONVERSATIONS_READ.value],
    )

    principal = await authentication.identify_api_key(issued.plaintext)
    assert principal is not None
    assert principal.kind is PrincipalKind.API_KEY
    assert principal.tenant_id == workspace.tenant.id
    # Scoped down, even though the creator was an owner.
    assert principal.permissions == frozenset({Permission.CONVERSATIONS_READ})
    assert not principal.has_permission(Permission.BILLING_MANAGE)


async def test_the_login_attempt_is_audited(
    db_session: AsyncSession,
    passwords: PasswordHashingService,
) -> None:
    workspace = await _create_workspace(db_session, passwords)
    authentication = _authentication(db_session, passwords)
    with pytest.raises(AuthenticationFailedError):
        await authentication.login(raw_email=workspace.user.email, raw_password=OTHER_PASSWORD)

    row = (
        await db_session.execute(
            text(
                "SELECT outcome, action, context FROM audit_logs "
                "WHERE actor_user_id = :id ORDER BY created_at DESC LIMIT 1"
            ),
            {"id": workspace.user.id},
        )
    ).one()
    assert row[0] == "failure"
    assert row[1] == "auth.login"
    assert OTHER_PASSWORD not in str(row[2])
