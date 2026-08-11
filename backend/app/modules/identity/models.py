"""Identity and access tables.

Conventions follow `docs/database.md` 3: UUID primary keys, `timestamptz`
columns that are always UTC, `created_at` on every row, `updated_at` on mutable
rows, and composite indexes that lead with `tenant_id`.

Four choices are worth explaining because the obvious alternative is worse:

* **UUIDv7, generated in Python.** `docs/database.md` mandates UUID keys.
  Generating them client-side means an insert knows its own identifier before
  it reaches the database, which matters for audit rows written in the same
  unit of work. UUIDv7 keeps those keys roughly time-ordered so the primary key
  index does not degenerate into random-write churn.
* **Status columns are `text` + `CHECK`, not PostgreSQL `ENUM`.** Adding a
  value to a PG enum type is a migration that takes a lock and cannot be run
  inside a transaction on older versions; changing a CHECK is an ordinary,
  reviewable migration.
* **`users.email` is `text`, not `citext`.** The Phase 2 migration creates a
  fixed extension set as a non-superuser role and CI pre-creates exactly those
  four extensions. Normalisation happens in `domain.normalize_email` and the
  unique index enforces it, with a CHECK constraint proving the invariant at
  the storage layer. See ADR-0015.
* **No plaintext credential column exists anywhere.** `users.password_hash`
  holds an Argon2id PHC string; `sessions.token_digest`, `api_keys.secret_digest`
  and `email_tokens.token_digest` hold SHA-256 digests of high-entropy values.

Cascade behaviour is chosen per relationship rather than uniformly: deleting a
tenant removes everything scoped to it, but audit rows survive with a nulled
reference, because an audit trail that disappears with the thing it indicts is
not an audit trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.platform.ids import new_uuid7


class IdentityRecord(Base):
    """Append-only row: UUIDv7 primary key and a creation timestamp."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MutableIdentityRecord(IdentityRecord):
    """Mutable row: adds `updated_at`, maintained by the ORM on flush."""

    __abstract__ = True

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Tenant(MutableIdentityRecord):
    """A customer organisation. This row *is* the isolation boundary."""

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug"),
        CheckConstraint("status IN ('active', 'suspended')", name="status"),
        CheckConstraint("slug = lower(slug)", name="slug_normalised"),
        Index("tenants_status_idx", "status"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    # Soft delete: a tenant carries billing history and audit rows that must
    # outlive it, so deletion is a state change and every read filters it out.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(MutableIdentityRecord):
    """A person's platform-wide account.

    Users are global, not tenant-scoped: the same person can belong to several
    tenants with one set of credentials. Everything tenant-specific about them
    lives on `Membership`.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email"),
        CheckConstraint("email = lower(email)", name="email_normalised"),
        CheckConstraint(
            "status IN ('active', 'suspended', 'deactivated')",
            name="status",
        ),
        CheckConstraint("session_epoch >= 0", name="session_epoch"),
        CheckConstraint("failed_logins >= 0", name="failed_logins"),
    )

    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    # Incremented to invalidate every existing session at once - on password
    # change, on "sign out everywhere", on suspension. Comparing this to the
    # value captured in a session makes revocation O(1) and, crucially,
    # unforgeable from the client side.
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Membership(MutableIdentityRecord):
    """One person's access to one tenant. Roles attach here, not to the user."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        CheckConstraint(
            "status IN ('invited', 'active', 'suspended')",
            name="status",
        ),
        Index("memberships_user_id_idx", "user_id"),
        Index("memberships_tenant_id_status_idx", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'invited'"))
    # SET NULL rather than CASCADE: removing the person who sent an invitation
    # must not remove the membership that invitation created.
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(MutableIdentityRecord):
    """A named bundle of permissions.

    `tenant_id IS NULL` marks a system role shared by every tenant; a non-null
    `tenant_id` marks a future custom role owned by one tenant. Two partial
    unique indexes enforce slug uniqueness in each namespace separately,
    because PostgreSQL treats NULLs as distinct and a plain
    `UNIQUE (tenant_id, slug)` would happily accept two system roles called
    `owner`.
    """

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("(tenant_id IS NULL) = is_system", name="system_roles_have_no_tenant"),
        Index("roles_slug_idx", "slug", unique=True, postgresql_where=text("tenant_id IS NULL")),
        Index(
            "roles_tenant_id_slug_idx",
            "tenant_id",
            "slug",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class PermissionRecord(IdentityRecord):
    """A permission slug. Global reference data, seeded by the migration.

    Named `PermissionRecord` so it cannot be confused with the `Permission`
    enum in `domain`, which is the source of truth this table mirrors.
    """

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("slug"),)

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class RolePermission(Base):
    """Which permissions a role grants."""

    __tablename__ = "role_permissions"
    __table_args__ = (Index("role_permissions_permission_id_idx", "permission_id"),)

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MembershipRole(Base):
    """Which roles a membership holds.

    `role_id` uses `RESTRICT`: a role that is still assigned to somebody cannot
    be deleted out from under them, which would silently strip permissions.
    Cross-tenant assignment is prevented in `RoleRepository`, which only ever
    resolves roles that are either system-wide or owned by the acting tenant.
    """

    __tablename__ = "membership_roles"
    __table_args__ = (Index("membership_roles_role_id_idx", "role_id"),)

    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memberships.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserSession(IdentityRecord):
    """A browser session. PostgreSQL is the record of truth (ADR-0009).

    Redis caches lookups but never owns them: a Redis flush must log people out
    of nothing, and a revocation must not depend on a cache eviction landing.

    `tenant_id` is the *active* tenant, chosen server-side at login or by an
    explicit switch. Request handling reads the tenant from here and never from
    client input, which is what makes tenant confusion unreachable rather than
    merely discouraged.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_digest"),
        Index("sessions_user_id_idx", "user_id"),
        Index("sessions_tenant_id_user_id_idx", "tenant_id", "user_id"),
        Index("sessions_absolute_expires_at_idx", "absolute_expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
    )
    token_digest: Mapped[str] = mapped_column(Text, nullable=False)
    # Double-submit CSRF: the digest is stored, the value goes to a readable
    # cookie, and the header must reproduce it.
    csrf_digest: Mapped[str] = mapped_column(Text, nullable=False)
    # Copied from `users.session_epoch` at creation. A mismatch means the user
    # invalidated everything since.
    user_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class ApiKey(MutableIdentityRecord):
    """A machine credential, always scoped to exactly one tenant.

    `key_id` is the public half and is indexed, so authenticating a key is one
    lookup rather than a scan that Argon2-hashes every stored row.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_id"),
        Index("api_keys_tenant_id_created_at_idx", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    secret_digest: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailToken(IdentityRecord):
    """A single-use token delivered by email (verification, credential reset).

    Stored as a digest and consumed by setting `consumed_at`, so replaying a
    token that was captured in transit fails on the second use.
    """

    __tablename__ = "email_tokens"
    __table_args__ = (
        UniqueConstraint("token_digest"),
        CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="purpose",
        ),
        Index("email_tokens_user_id_purpose_idx", "user_id", "purpose"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    token_digest: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(IdentityRecord):
    """Append-only security trail.

    No `updated_at` and no delete path: an audit row is a statement about what
    happened, and editing history is exactly what it exists to prevent.
    Actor references are `SET NULL` so the trail survives the removal of the
    person or key it describes.

    `context` holds structured details and is written only by
    `AuditService`, which never receives credential material to begin with.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("outcome IN ('success', 'failure')", name="outcome"),
        Index("audit_logs_tenant_id_created_at_idx", "tenant_id", "created_at"),
        Index("audit_logs_actor_user_id_created_at_idx", "actor_user_id", "created_at"),
        Index("audit_logs_action_created_at_idx", "action", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
