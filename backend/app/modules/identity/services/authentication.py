"""Credential verification and principal resolution.

Every rejection on the login path raises the same `AuthenticationFailedError`
with the same message. Unknown account, wrong password, locked account,
suspended account and stale session are indistinguishable to the caller. The
reason is recorded in the audit row and the internal message, where an operator
can see it and an attacker cannot.

The unknown-account path deliberately spends the same CPU as a real Argon2id
verification. Without that, response time answers "does this email have an
account here?" for free.

The tenant a session acts in is chosen here, server-side, and stored on the
session row. Nothing downstream reads a tenant from a header or a body, so
there is no request field an attacker can edit to move sideways.

The authority a session carries is resolved from PostgreSQL through
`PermissionResolver`, never from a Python grant table. `permissions_for_roles`
is deliberately absent from this module's imports.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitedError
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordHashingService
from app.core.settings import AuthSettings
from app.modules.identity import models
from app.modules.identity.domain import (
    AuditOutcome,
    MembershipStatus,
    Permission,
    Principal,
    PrincipalKind,
    TenantContext,
    TenantStatus,
    UserStatus,
    normalize_email,
    normalize_tenant_slug,
)
from app.modules.identity.errors import AuthenticationFailedError
from app.modules.identity.repositories import (
    MembershipRepository,
    TenantRepository,
    UserRepository,
)
from app.modules.identity.services.api_keys import ApiKeyService
from app.modules.identity.services.audit import AuditService
from app.modules.identity.services.permissions import (
    EffectivePermissionCache,
    PermissionResolver,
)
from app.modules.identity.services.sessions import IssuedSession, SessionService
from app.platform.clock import is_expired, seconds_from_now, utcnow


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """A live session, the person behind it and their authority.

    `principal` is None for a signed-in person with no active tenant - someone
    who has registered but not yet created or accepted a workspace. They can
    still call the endpoints that exist to fix that, and nothing else.
    """

    record: models.UserSession
    user: models.User
    principal: Principal | None


@dataclass(frozen=True, slots=True)
class LoginResult:
    """The outcome of a successful password exchange."""

    user: models.User
    issued: IssuedSession


class AuthenticationService:
    """Turns credentials into sessions, and credentials into principals."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: AuthSettings,
        passwords: PasswordHashingService,
        sessions: SessionService,
        api_keys: ApiKeyService,
        cache: EffectivePermissionCache,
        audit: AuditService,
        login_limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._passwords = passwords
        self._sessions = sessions
        self._api_keys = api_keys
        self._cache = cache
        self._audit = audit
        # Optional so that a caller with no Redis - the test application, a
        # future CLI - still gets a working service with the per-account
        # lockout intact, rather than a hard dependency on a cache.
        self._login_limiter = login_limiter
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)

    async def login(
        self,
        *,
        raw_email: str,
        raw_password: str,
        tenant_slug: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        """Verify a password and issue a session, or fail indistinguishably."""
        await self._enforce_source_rate_limit(ip_address)

        try:
            email = normalize_email(raw_email)
        except ValueError:
            # A malformed address cannot match a stored one. Spend the budget
            # anyway so "not an email" and "no such account" cost the same.
            email = None

        user = None if email is None else await self._users.get_by_email(email)
        if user is None:
            self._passwords.spend_verification_budget()
            await self._audit_login(outcome=AuditOutcome.FAILURE, ip_address=ip_address)
            raise self._rejected("no account matches the presented address")

        now = utcnow()
        # `locked_until` is None when the account has never been locked. Unlike
        # an expiry deadline, where None means "never expires", None here means
        # "not locked" - so it must not be read through is_expired alone, which
        # would treat the absent lock as a lock that never lifts. Reject only
        # while a real lock is still in the future.
        if user.locked_until is not None and not is_expired(user.locked_until, now=now):
            self._passwords.spend_verification_budget()
            await self._audit_login(
                outcome=AuditOutcome.FAILURE,
                user=user,
                ip_address=ip_address,
                reason="locked_out",
            )
            raise self._rejected(f"account {user.id} is locked until {user.locked_until}")

        verification = self._passwords.verify(
            stored_hash=user.password_hash,
            candidate=raw_password,
        )
        if not verification.matched:
            await self._users.record_failed_login(
                user,
                max_failures=self._settings.max_failed_logins,
                lockout_until=seconds_from_now(self._settings.lockout_seconds),
            )
            await self._audit_login(
                outcome=AuditOutcome.FAILURE,
                user=user,
                ip_address=ip_address,
                reason="bad_credentials",
            )
            raise self._rejected(f"password mismatch for account {user.id}")

        if user.status != UserStatus.ACTIVE.value:
            # Checked after verification on purpose: answering earlier would
            # reveal account state to someone who does not know the password.
            await self._audit_login(
                outcome=AuditOutcome.FAILURE,
                user=user,
                ip_address=ip_address,
                reason="inactive_account",
            )
            raise self._rejected(f"account {user.id} has status {user.status}")

        if verification.needs_rehash:
            # Transparent upgrade: the cost parameters were raised since this
            # password was last set, and we have the plaintext exactly here.
            user.password_hash = self._passwords.hash(raw_password)

        await self._users.record_successful_login(user, now=now)
        tenant_id = await self._select_tenant(user, tenant_slug)
        issued = await self._sessions.issue(
            user=user,
            tenant_id=tenant_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._audit_login(
            outcome=AuditOutcome.SUCCESS,
            user=user,
            tenant_id=tenant_id,
            ip_address=ip_address,
        )
        return LoginResult(user=user, issued=issued)

    async def _enforce_source_rate_limit(self, ip_address: str | None) -> None:
        """Throttle login attempts by source address (`docs/security.md` 2.5).

        Three properties are deliberate:

        * **It counts before anything else happens.** The attempt is refused
          before the address is looked up and before Argon2 runs, so a flood
          costs the attacker a request and costs us one Redis `INCR`.
        * **It counts every attempt, whatever account it names and whether or
          not it succeeds.** A per-account counter is stepped around by
          changing the account - which is precisely what credential stuffing
          does - so the per-source counter must not be resettable by trying a
          different victim, or by occasionally guessing right.
        * **It complements, never replaces, the lockout.** When the limiter is
          absent or Redis is unreachable, `users.failed_logins` and
          `locked_until` are untouched and still stop a focused attack on one
          account.

        A request with no resolvable source address is not throttled, because
        the alternative - bucketing every such request together - would let one
        client with a hidden address lock out all of them. `_client_ip` in the
        routes is the only thing that produces this value, and it never trusts
        a forwarding header unless the deployment says how many hops to trust.
        """
        if self._login_limiter is None or ip_address is None:
            return
        decision = await self._login_limiter.hit(ip_address)
        if decision.allowed:
            return
        await self._audit_login(
            outcome=AuditOutcome.FAILURE,
            ip_address=ip_address,
            reason="source_rate_limited",
        )
        raise RateLimitedError(
            details={"retry_after_seconds": decision.retry_after_seconds},
            internal_message=(
                "login attempts from this source address exceeded the configured window"
            ),
        )

    async def identify_session(self, token: str) -> SessionIdentity | None:
        """Resolve a presented session token to its identity, or None."""
        record = await self._sessions.find_live(token)
        if record is None:
            return None

        user = await self._users.get_by_id(record.user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            return None
        if not self._sessions.matches_user_epoch(record, user):
            # The user bumped their epoch - a global sign-out or a credential
            # change - after this session was issued.
            return None

        await self._sessions.touch_if_stale(record)
        principal = await self._principal_for_session(record, user)
        return SessionIdentity(record=record, user=user, principal=principal)

    async def identify_api_key(self, presented: str) -> Principal | None:
        """Resolve a presented API key to a tenant-scoped principal, or None."""
        record = await self._api_keys.authenticate(presented)
        if record is None:
            return None

        tenant = await self._tenants.get_by_id(record.tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            return None

        # A key carries scopes, not roles. Widening a role later must not
        # silently widen every key that was issued under it. Scopes were
        # already intersected against the creator's effective permissions at
        # issue time, so this path never consulted a role grant table.
        scopes = {str(scope) for scope in record.scopes}
        permissions = frozenset(item for item in Permission if item.value in scopes)
        return Principal(
            kind=PrincipalKind.API_KEY,
            tenant_id=tenant.id,
            permissions=permissions,
            role_slugs=frozenset(),
            api_key_id=record.id,
        )

    async def logout(self, record: models.UserSession) -> None:
        """Revoke a single session."""
        await self._sessions.revoke(record)
        await self._audit.record(
            action="auth.logout",
            resource_type="session",
            resource_id=str(record.id),
            outcome=AuditOutcome.SUCCESS,
            tenant_id=record.tenant_id,
            actor_user_id=record.user_id,
        )

    async def logout_everywhere(self, user: models.User) -> int:
        """Revoke every session for a user and invalidate any future replay.

        Revoking the rows is not enough on its own: bumping the epoch means a
        session row that is somehow missed - a replica lag, a race with an
        in-flight request - still fails the epoch check on its next use.
        """
        revoked = await self._sessions.revoke_all_for_user(user.id)
        await self._users.bump_session_epoch(user)
        await self._audit.record(
            action="auth.logout_all",
            resource_type="user",
            resource_id=str(user.id),
            outcome=AuditOutcome.SUCCESS,
            actor_user_id=user.id,
            context={"revoked_sessions": revoked},
        )
        return revoked

    def _rejected(self, internal_message: str) -> AuthenticationFailedError:
        """Build the one error every login failure raises."""
        return AuthenticationFailedError(internal_message=internal_message)

    async def _audit_login(
        self,
        *,
        outcome: AuditOutcome,
        user: models.User | None = None,
        tenant_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Append one login audit row.

        The presented address is not recorded on the unknown-account path: the
        audit log would otherwise accumulate a list of addresses that someone
        guessed, which is a new place for personal data to leak from.
        """
        await self._audit.record(
            action="auth.login",
            resource_type="session",
            resource_id=None if user is None else str(user.id),
            outcome=outcome,
            tenant_id=tenant_id,
            actor_user_id=None if user is None else user.id,
            ip_address=ip_address,
            context=None if reason is None else {"reason": reason},
        )

    async def _select_tenant(
        self,
        user: models.User,
        tenant_slug: str | None,
    ) -> uuid.UUID | None:
        """Choose the tenant this session will act in.

        An unusable slug produces a tenantless session rather than an error.
        Distinguishing "no such workspace" from "not your workspace" would turn
        the login form into a lookup service for customer names.

        An `INVITED` membership is not usable here, which is what makes
        acceptance meaningful: until the invitation is accepted or activated,
        signing in with that workspace's slug produces a tenantless session
        rather than a privileged one.
        """
        if tenant_slug is None:
            return None
        try:
            slug = normalize_tenant_slug(tenant_slug)
        except ValueError:
            return None

        tenant = await self._tenants.get_by_slug(slug)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            return None

        memberships = MembershipRepository(self._session, TenantContext(tenant_id=tenant.id))
        membership = await memberships.get_for_user(user.id)
        if membership is None or membership.status != MembershipStatus.ACTIVE.value:
            return None
        return tenant.id

    async def _principal_for_session(
        self,
        record: models.UserSession,
        user: models.User,
    ) -> Principal | None:
        """Resolve the authority a session currently has.

        Recomputed per request rather than stored on the session, so revoking
        a role takes effect on the next request instead of whenever the
        session happens to expire.

        The permission set comes from the resolver, which reads
        `role_permissions` in PostgreSQL. Both calls below share one resolved
        value through the resolver's cache, so the roles reported and the
        permissions granted are always the same generation of the truth.
        """
        if record.tenant_id is None:
            return None

        tenant = await self._tenants.get_by_id(record.tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE.value:
            return None

        tenant_context = TenantContext(tenant_id=tenant.id)
        memberships = MembershipRepository(self._session, tenant_context)
        membership = await memberships.get_for_user(user.id)
        if membership is None or membership.status != MembershipStatus.ACTIVE.value:
            return None

        resolver = PermissionResolver(memberships, self._cache)
        role_slugs = await resolver.role_slugs_for(membership.id)
        permissions = await resolver.permissions_for(membership.id)
        return Principal(
            kind=PrincipalKind.USER,
            tenant_id=tenant.id,
            permissions=permissions,
            role_slugs=role_slugs,
            user_id=user.id,
            session_id=record.id,
            membership_id=membership.id,
        )
