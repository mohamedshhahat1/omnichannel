"""Creating tenants, accounts and memberships.

The invariant this module exists to protect is that a tenant is never created
without an active owner. A tenant with no owner cannot be administered, cannot
be billed and cannot be deleted through the product - it has to be repaired by
hand in the database. So tenant creation, the owner's membership and the owner
role assignment all happen in one unit of work, and the caller gets all three
or none.

The membership lifecycle lives here too. `invite_member` creates an `INVITED`
row; `accept_invitation` and `activate_membership` are the only two ways it
becomes `ACTIVE`, and both funnel through `_transition_to_active` so the rules -
which states may move, what happens to the cache, when `accepted_at` is
stamped - are written once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.breached_passwords import is_breached
from app.core.security import PasswordHashingService
from app.core.settings import AuthSettings
from app.modules.identity import models
from app.modules.identity.domain import (
    MembershipStatus,
    Permission,
    Principal,
    RoleSlug,
    TenantContext,
    TenantStatus,
    check_password_policy,
    normalize_display_name,
    normalize_email,
    normalize_tenant_slug,
)
from app.modules.identity.errors import (
    IdentityValidationError,
    LastOwnerError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MembershipTransitionError,
    RoleNotFoundError,
    TenantNotFoundError,
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
from app.modules.identity.services.permissions import (
    EffectivePermissionCache,
    PermissionResolver,
)
from app.platform.clock import utcnow


@dataclass(frozen=True, slots=True)
class ProvisionedTenant:
    """A new tenant together with its owner's membership."""

    tenant: models.Tenant
    membership: models.Membership


@dataclass(frozen=True, slots=True)
class AcceptedInvitation:
    """The result of an invitee accepting their own invitation.

    Carries the role slugs because the caller has no tenant-scoped principal
    yet - it is the acceptance that creates one - and therefore no resolver to
    ask afterwards.
    """

    tenant: models.Tenant
    membership: models.Membership
    role_slugs: frozenset[str]


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

    def _enforce_password_policy(self, raw_password: str) -> None:
        """Apply every rule a new password must satisfy.

        Length and whitespace come from the domain policy. The breach screen is
        `docs/security.md` 2.5's second requirement and is applied here, at the
        single point where both registration and invitation turn a plaintext
        into a stored hash, so neither route can acquire a weak credential the
        other refuses.

        The password is never logged, echoed, or included in the error. The
        caller learns only that this password is unusable.
        """
        validation.enforce(
            "password",
            lambda: check_password_policy(
                raw_password,
                min_length=self._settings.password_min_length,
                max_length=self._settings.password_max_length,
            ),
        )
        if self._settings.breach_screen_enabled and is_breached(raw_password):
            raise IdentityValidationError(
                "That password appears in known breach data. Please choose another.",
                details={"field": "password", "reason": "breached"},
                internal_message="password rejected by the local breach screen",
            )

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
        self._enforce_password_policy(raw_password)
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

        The membership is created `INVITED` and confers nothing until it is
        accepted (`accept_invitation`) or activated by a member manager
        (`activate_membership`). That is the whole point of the status: an
        address someone typed is not yet a person who has agreed to join.
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

    async def _transition_to_active(
        self,
        memberships: MembershipRepository,
        membership: models.Membership,
        *,
        resolver: PermissionResolver,
    ) -> models.Membership:
        """Move one membership to ACTIVE, or explain why it cannot move.

        The only legal source state is `INVITED`. Two deliberate choices:

        * An already-active membership is a no-op that succeeds. Acceptance
          arrives over a network from a person clicking a link; a retry, a
          double submit or a replayed request must not turn into an error the
          user has to understand, and there is nothing to change.
        * A `SUSPENDED` membership is refused. Reinstating suspended access is
          a different decision, taken by a different person, and letting
          "accept your invitation" silently perform it would make suspension
          reversible by the suspended party.

        The status write and the cache invalidation happen inside the caller's
        transaction, so a failure anywhere in the request leaves the membership
        exactly as it was.
        """
        current = membership.status
        if current == MembershipStatus.ACTIVE.value:
            return membership
        if current != MembershipStatus.INVITED.value:
            active = MembershipStatus.ACTIVE.value
            message = f"membership {membership.id} cannot move {current} -> {active}"
            raise MembershipTransitionError(
                details={"status": current},
                internal_message=message,
            )
        await memberships.set_status(membership, MembershipStatus.ACTIVE)
        # Nothing cached is wrong yet - the cache holds roles and permissions,
        # not status - but a membership that has just become able to hold a
        # principal should start from a clean read rather than from whatever a
        # previous resolution left behind.
        await resolver.invalidate(membership.id)
        return membership

    async def accept_invitation(
        self,
        *,
        user: models.User,
        raw_tenant_slug: str,
        cache: EffectivePermissionCache,
    ) -> AcceptedInvitation:
        """Let an invited person accept their own invitation.

        Authorisation is identity, not permission: the only membership this can
        touch is the caller's own, in the tenant they name, found through a
        tenant-scoped repository. There is no membership id in the signature, so
        there is no id to tamper with - a caller cannot accept on behalf of
        somebody else even by guessing one.

        An unknown slug, a suspended tenant and "you were never invited here"
        all answer `tenant_not_found`, exactly as `authorization.py` requires:
        a stranger must not be able to use this endpoint to discover which
        workspaces exist.
        """
        slug = validation.normalized("slug", raw_tenant_slug, normalize_tenant_slug)
        tenant = await self._tenants.get_by_slug(slug)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            raise TenantNotFoundError(
                internal_message=f"no active tenant for slug {slug} on invitation acceptance",
            )

        memberships = MembershipRepository(self._session, TenantContext(tenant_id=tenant.id))
        membership = await memberships.get_for_user(user.id)
        if membership is None:
            message = f"user {user.id} has no membership in tenant {tenant.id}"
            raise TenantNotFoundError(internal_message=message)

        resolver = PermissionResolver(memberships, cache)
        membership = await self._transition_to_active(
            memberships,
            membership,
            resolver=resolver,
        )
        return AcceptedInvitation(
            tenant=tenant,
            membership=membership,
            role_slugs=await memberships.role_slugs_for(membership.id),
        )

    async def activate_membership(
        self,
        *,
        principal: Principal,
        membership_id: uuid.UUID,
        resolver: PermissionResolver,
    ) -> models.Membership:
        """Activate an invited membership on behalf of a member manager.

        The administrative counterpart of `accept_invitation`, for the operator
        who onboards a colleague in person rather than by link. It requires
        `members.manage` - the same permission that can already change what a
        member may do - and it is tenant scoped, so a membership id belonging to
        another tenant is *not found* rather than *forbidden*.
        """
        require_permission(principal, Permission.MEMBERS_MANAGE)
        memberships = MembershipRepository(self._session, principal.tenant)
        membership = await memberships.get_by_id(membership_id)
        if membership is None:
            message = f"membership {membership_id} is not in tenant {principal.tenant_id}"
            raise MembershipNotFoundError(internal_message=message)
        return await self._transition_to_active(memberships, membership, resolver=resolver)

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

    async def remove_role(
        self,
        *,
        principal: Principal,
        membership_id: uuid.UUID,
        role_slug: str,
        resolver: PermissionResolver,
    ) -> models.Membership:
        """Take a role away from a membership in the caller's tenant.

        The missing half of `assign_role`. Without it a role could be granted
        through the API but only removed with SQL, which means de-privileging
        somebody during an incident depended on database access - the one thing
        an incident response should not require.

        Two guards beyond the permission check:

        * Removing `owner` needs `billing.manage` as well, mirroring the rule
          on granting it, so the ability to remove an owner is not a cheaper
          way to reach ownership than the ability to create one.
        * The last **active** owner cannot be stripped. A tenant with no owner
          cannot be administered or billed and has to be repaired by hand -
          exactly the state `create_tenant_with_owner` exists to prevent.

        Invalidating the cache afterwards is mandatory, not decorative: the
        contract in `security.md` 3.2 is that a mutation takes effect on the
        next request rather than when a TTL happens to lapse.
        """
        require_permission(principal, Permission.MEMBERS_MANAGE)
        context = principal.tenant
        memberships = MembershipRepository(self._session, context)
        roles = RoleRepository(self._session, context)

        membership = await memberships.get_by_id(membership_id)
        if membership is None:
            raise MembershipNotFoundError(
                internal_message=f"membership {membership_id} not in tenant {context.tenant_id}",
            )
        role = await roles.get_by_slug(role_slug)
        if role is None:
            raise RoleNotFoundError(internal_message=f"unknown or foreign role slug: {role_slug}")

        if role.slug == RoleSlug.OWNER.value:
            require_permission(principal, Permission.BILLING_MANAGE)
            if await memberships.count_active_owners(role.id) <= 1:
                message = f"refusing to remove the last active owner of {context.tenant_id}"
                raise LastOwnerError(internal_message=message)

        removed = await memberships.remove_role(membership_id=membership.id, role_id=role.id)
        if not removed:
            message = f"membership {membership.id} does not hold role {role.slug}"
            raise RoleNotFoundError(internal_message=message)
        await resolver.invalidate(membership.id)
        return membership
