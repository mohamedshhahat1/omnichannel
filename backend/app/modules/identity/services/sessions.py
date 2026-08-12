"""Session issuance, resolution and revocation.

PostgreSQL is the record of truth for sessions (ADR-0009). That is the whole
reason revocation works: a revoked row is revoked for every process
immediately, with no cache entry to expire and no window in which one node
still honours a session another node has killed.

A session has three independent kill switches, because each covers a case the
others do not:

* `revoked_at` - this session, right now (sign out).
* `idle_expires_at` - a session nobody is using (walked-away laptop).
* `absolute_expires_at` - a session that has simply existed too long,
  regardless of activity (a stolen token being kept warm).

A fourth lives on the user: `session_epoch`. Bumping it invalidates every
session that person holds in one write, which is what makes "change my
password" and "sign out everywhere" instant rather than a loop over rows.

Only digests are stored. A database dump therefore contains nothing that can be
replayed as a session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.security import digest_secret, generate_token, secrets_equal
from app.core.settings import AuthSettings
from app.modules.identity import models
from app.modules.identity.repositories import SessionRepository
from app.platform.clock import utcnow


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly created session.

    `token` and `csrf_token` are the only plaintext copies that will ever
    exist. They go straight into `Set-Cookie` headers and are then unreachable.
    """

    session_id: uuid.UUID
    token: str
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


class SessionService:
    """Owns the session lifecycle."""

    def __init__(self, sessions: SessionRepository, settings: AuthSettings) -> None:
        self._sessions = sessions
        self._settings = settings

    async def issue(
        self,
        *,
        user: models.User,
        tenant_id: uuid.UUID | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Create a session for a user and return its plaintext halves.

        The active tenant is fixed here, server-side. Nothing downstream reads
        a tenant from the request, so a caller cannot talk their way into
        another tenant by editing a header.
        """
        now = utcnow()
        token = generate_token()
        csrf_token = generate_token()
        idle_expires_at = now + timedelta(seconds=self._settings.session_idle_ttl_seconds)
        absolute_expires_at = now + timedelta(
            seconds=self._settings.session_absolute_ttl_seconds,
        )
        record = await self._sessions.create(
            user_id=user.id,
            tenant_id=tenant_id,
            token_digest=digest_secret(token),
            csrf_digest=digest_secret(csrf_token),
            user_epoch=user.session_epoch,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return IssuedSession(
            session_id=record.id,
            token=token,
            csrf_token=csrf_token,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )

    async def find_live(self, presented_token: str) -> models.UserSession | None:
        """Return the live session for a presented token, or None.

        "None" covers unknown, revoked, idle-expired and absolutely expired in
        one answer on purpose: the caller has no legitimate use for the
        difference, and neither has an attacker.
        """
        record = await self._sessions.get_by_digest(digest_secret(presented_token))
        if record is None:
            return None
        now = utcnow()
        if record.revoked_at is not None:
            return None
        if record.idle_expires_at <= now or record.absolute_expires_at <= now:
            return None
        return record

    def matches_user_epoch(self, record: models.UserSession, user: models.User) -> bool:
        """True when the session predates no bulk invalidation."""
        return record.user_epoch == user.session_epoch

    def verify_csrf(self, record: models.UserSession, presented: str | None) -> bool:
        """Constant-time double-submit check for a state-changing request."""
        if not presented:
            return False
        return secrets_equal(record.csrf_digest, digest_secret(presented))

    async def touch_if_stale(self, record: models.UserSession) -> None:
        """Slide the idle window, at most once per touch interval.

        Rate-limited deliberately: refreshing on every request would turn a
        read-only page load into a write and put every active session's row in
        contention on a busy account.
        """
        now = utcnow()
        interval = timedelta(seconds=self._settings.session_touch_interval_seconds)
        if record.last_seen_at + interval > now:
            return
        idle_expires_at = now + timedelta(seconds=self._settings.session_idle_ttl_seconds)
        capped = min(idle_expires_at, record.absolute_expires_at)
        await self._sessions.touch(record, now=now, idle_expires_at=capped)

    async def revoke(self, record: models.UserSession) -> None:
        """Revoke a single session."""
        await self._sessions.revoke(record, now=utcnow())

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke every live session for a user and return the count."""
        return await self._sessions.revoke_all_for_user(user_id, now=utcnow())
