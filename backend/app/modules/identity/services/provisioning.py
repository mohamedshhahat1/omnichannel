"""Creating tenants, accounts and memberships.

The invariant this module exists to protect is that a tenant is never created
without an active owner. A tenant with no owner cannot be administered, cannot
be billed and cannot be deleted through the product - it has to be repaired by
hand in the database. So tenant creation, the owner's membership and the owner
role assignment all happen in one unit of work, and the caller gets all three
or none.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import PasswordHashingService
from app.core.settings import AuthSettings
from app.modules.identity import models
from app.modules.identity.domain import (
    MembershipStatus,
    Permission,
    Principal,
    RoleSlug,
    TenantContext,
    check_password_policy,
    normalize_display_name,
    normalize_email,
    normalize_tenant_slug,
)
from app.modules.identity.errors import (
    MembershipAlreadyExistsError,
    RoleNotFoundError,
    TenantSlugTakenError,
)
from app.modules.identity.repositories import (
    MembershipRepository,
    RoleRepository,
    TenantRepository,
    UserRepository,
)
from app.modules.identity.services import validation
from app.modules.identity.services.audit import AuditService
from app.modules.identity.services.authorization import require_permission
from app.modules.identity.services.permissions import PermissionResolver
from app.platform.clock import utcnow


@dataclass(frozen=True, slots=True)
class ProvisionedTenant:
    """A new tenant together with its owner's membership."""

    tenant: models.Tenant
    membership: models.Membership


class ProvisioningService:
    """Tenant, account and membership creation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        passwords: PasswordHashingService,
        settings: AuthSettings,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._passwords = passwords
        self._settings = settings
        self._audit = audit
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)

    async def find_user_by_email(self, raw_email: str) -> models.User | None:
        """Look up an account by its raw, un-normalised address."""
        email = validation.normalized("email", raw_email, normalize_email)
        return await self._users.get_by_email(email)

    async def assert_slug_available(self, raw_slug: str) -> str:
        """Normalise a tenant slug and reject it if it is already taken.

        Separate from `create_tenant_with_owner` so registration can check the
        slug *before* it branches on whether the email exists. If the conflict
        surfaced only on the create path, an attacker could submit a slug they
        know is taken and read the difference between 409 and 202 as "is this
        email already registered?" - reintroducing the enumeration oracle that
        the generic 202 exists to close.
        """
        slug = validation.normalized("slug", raw_slug, normalize_tenant_slug)
        if await self._tenants.get_by_slug(slug) is not None:
            raise TenantSlugTakenError(internal_message=f"tenant slug already in use: {slug}")
        return slug

    async def create_user(
        self,
        *,
        raw_email: str,
        raw_password: str,
        raw_display_name: str,
    ) -> models.User:
        """Create an account, hashing the password before anything is stored.

        The plaintext is a local that goes out of scope at the end of this
        call. It is never assigned to the model, so it cannot be picked up by
        an ORM repr, a log record or a traceback frame that renders locals.
        """
        email = validation.normalized("email", raw_email, normalize_email)
        display_name = validation.normalized(
            "display_name",
            raw_display_name,
            normalize_display_name,
        )
        validation.enforce(
            "password",
            lambda: check_password_policy(
                raw_password,
                min_length=self._settings.password_min_length,
                max_length=self._settings.password_max_length,
            ),
        )
        return await self._users.create(
            email=email,
            password_hash=self._passwords.hash(raw_password),
            display_name=display_name,
        )

    async def create_tenant_with_owner(
        self,
        *,
        owner: models.User,
        raw_name: str,
        raw_slug: str,
    ) -> ProvisionedTenant:
        """Create a tenant and make `owner` its first active owner."""
        name = validation.normalized("name", raw_name, normalize_display_name)
        slug = validation.normalized("slug", raw_slug, normalize_tenant_slug)

        if await self._tenants.get_by_slug(slug) is not None:
            raise TenantSlugTakenError(internal_message=f"tenant slug already in use: {slug}")

        tenant = await self._tenants.create(name=name, slug=slug)
        context = TenantContext(tenant_id=tenant.id)
        memberships = MembershipRepository(self._session, context)
        roles = RoleRepository(self._session, context)

        membership = await memberships.create(
            user_id=owner.id,
            status=MembershipStatus.ACTIVE,
            accepted_at=utcnow(),
        )
        owner_role = await roles.get_by_slug(RoleSlug.OWNER.value)
        if owner_role is None:
            # The system roles are seeded by migration 0002. Their absence is a
            # deployment fault, not a client error, and creating a tenant
            # without an owner role would be worse than failing.
            raise RoleNotFoundError(
                internal_message="system role 'owner' is missing; run alembic upgrade head",
            )
        await memberships.assign_role(membership_id=membership.id, role_id=owner_role.id)
        return ProvisionedTenant(tenant=tenant, membership=membership)

    async def invite_member(
        self,
        *,
        principal: Principal,
        raw_email: str,
        raw_display_name: str,
        raw_password: str,
        role_slug: str,
    ) -> models.Membership:
        """Add a person to the caller's tenant with one role.

        Phase 3 has no outbound email, so an invitation creates the account
        directly with a caller-supplied initial credential. `docs/security.md`
        already requires verification before privileged actions; wiring the
        delivery of that token is Phase 4 work and is recorded in TODO.md.
        """
        require_permission(principal, Permission.MEMBERS_INVITE)
        context = principal.tenant
        memberships = MembershipRepository(self._session, context)
        roles = RoleRepository(self._session, context)

        role = await roles.get_by_slug(role_slug)
        if role is None:
            raise RoleNotFoundError(internal_message=f"unknown or foreign role slug: {role_slug}")

        # Only an owner may mint another owner. Without this, any account with
        # members.invite could grant itself a colleague with billing rights and
        # then use them - a one-step privilege escalation.
        if role.slug == RoleSlug.OWNER.value:
            require_permission(principal, Permission.BILLING_MANAGE)
            require_permission(principal, Permission.MEMBERS_MANAGE)

        user = await self.find_user_by_email(raw_email)
        if user is None:
            user = await self.create_user(
                raw_email=raw_email,
                raw_password=raw_password,
                raw_display_name=raw_display_name,
            )
        elif await memberships.get_for_user(user.id) is not None:
            raise MembershipAlreadyExistsError(
                internal_message=f"user {user.id} already belongs to tenant {context.tenant_id}",
            )

        membership = await memberships.create(
            user_id=user.id,
            status=MembershipStatus.INVITED,
            invited_by_id=principal.user_id,
        )
        await memberships.assign_role(membership_id=membership.id, role_id=role.id)
        return membership

    async def list_members(
        self,
        *,
        principal: Principal,
        limit: int,
        offset: int,
    ) -> Sequence[models.Membership]:
        """List memberships in the caller's tenant."""
        require_permission(principal, Permission.TENANT_READ)
        memberships = MembershipRepository(self._session, principal.tenant)
        return await memberships.list_all(limit=limit, offset=offset)

    async def assign_role(
        self,
        *,
        principal: Principal,
        membership_id: uuid.UUID,
        role_slug: str,
        resolver: PermissionResolver,
    ) -> models.Membership:
        """Grant a role to an existing membership in the caller's tenant."""
        require_permission(principal, Permission.MEMBERS_MANAGE)
        context = principal.tenant
        memberships = MembershipRepository(self._session, context)
        roles = RoleRepository(self._session, context)

        membership = await memberships.get_by_id(membership_id)
        if membership is None:
            raise RoleNotFoundError(
                internal_message=f"membership {membership_id} not in tenant {context.tenant_id}",
            )
        role = await roles.get_by_slug(role_slug)
        if role is None:
            raise RoleNotFoundError(internal_message=f"unknown or foreign role slug: {role_slug}")
        if role.slug == RoleSlug.OWNER.value:
            require_permission(principal, Permission.BILLING_MANAGE)

        await memberships.assign_role(membership_id=membership.id, role_id=role.id)
        # The cached slug set is now wrong; drop it rather than wait out the
        # TTL, because the whole point of a privilege change is immediacy.
        await resolver.invalidate(membership.id)
        return membership
