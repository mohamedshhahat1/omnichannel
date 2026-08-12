"""Identity domain rules: permissions, role grants, principals, normalisation.

This module is pure. No SQLAlchemy, no FastAPI, no clock, no I/O. That is not
tidiness for its own sake: these are the rules that decide who may do what, and
they should be testable exhaustively in milliseconds.

The permission catalogue and the default role grants are transcribed from
`docs/security.md` 3 and are a deliberately closed set. Phase 3 implements the
RBAC model the project already specified; it does not build a general policy
engine that nothing yet needs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

MAX_EMAIL_LENGTH: Final = 254
MIN_SLUG_LENGTH: Final = 3
MAX_SLUG_LENGTH: Final = 50
MAX_DISPLAY_NAME_LENGTH: Final = 120

_EMAIL_PATTERN: Final = re.compile(r"\A[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+\Z")
_SLUG_PATTERN: Final = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class Permission(StrEnum):
    """The complete permission catalogue (`docs/security.md` 3).

    Permissions are verbs on resources, never role names. Code asks "may this
    principal reply to a conversation", never "is this principal an admin",
    which is what lets the role table change without touching enforcement.
    """

    TENANT_READ = "tenant.read"
    TENANT_UPDATE = "tenant.update"
    MEMBERS_INVITE = "members.invite"
    MEMBERS_MANAGE = "members.manage"
    CHANNELS_CONNECT = "channels.connect"
    CHANNELS_MANAGE = "channels.manage"
    CONVERSATIONS_READ = "conversations.read"
    CONVERSATIONS_REPLY = "conversations.reply"
    CONVERSATIONS_ASSIGN = "conversations.assign"
    AI_CONFIGURE = "ai.configure"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    CATALOG_MANAGE = "catalog.manage"
    BILLING_MANAGE = "billing.manage"
    APIKEYS_MANAGE = "apikeys.manage"
    AUDIT_READ = "audit.read"


class RoleSlug(StrEnum):
    """The six system roles seeded for every tenant."""

    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    BILLING_ADMIN = "billing_admin"
    VIEWER = "viewer"


class TenantStatus(StrEnum):
    """Lifecycle of a customer organisation."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserStatus(StrEnum):
    """Lifecycle of a person's platform-wide account."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class MembershipStatus(StrEnum):
    """Lifecycle of one person's access to one tenant."""

    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EmailTokenPurpose(StrEnum):
    """Why a single-use email token was issued."""

    VERIFY_EMAIL = "email_verification"
    RESET_CREDENTIAL = "password_reset"


class PrincipalKind(StrEnum):
    """What kind of actor is behind a request."""

    USER = "user"
    API_KEY = "api_key"


class AuditOutcome(StrEnum):
    """Whether an audited attempt succeeded."""

    SUCCESS = "success"
    FAILURE = "failure"


_ALL_PERMISSIONS: Final = frozenset(Permission)

_CONVERSATION_WORK: Final = frozenset(
    {
        Permission.CONVERSATIONS_READ,
        Permission.CONVERSATIONS_REPLY,
        Permission.CONVERSATIONS_ASSIGN,
    }
)

# Owner is the only role that can manage billing; Admin is otherwise complete.
# That single difference is what makes "Admin" safe to hand out.
DEFAULT_ROLE_GRANTS: Final[Mapping[RoleSlug, frozenset[Permission]]] = {
    RoleSlug.OWNER: _ALL_PERMISSIONS,
    RoleSlug.ADMIN: _ALL_PERMISSIONS - {Permission.BILLING_MANAGE},
    RoleSlug.MANAGER: _CONVERSATION_WORK
    | {
        Permission.TENANT_READ,
        Permission.CATALOG_MANAGE,
        Permission.KNOWLEDGE_MANAGE,
        Permission.AI_CONFIGURE,
    },
    RoleSlug.AGENT: _CONVERSATION_WORK | {Permission.TENANT_READ},
    RoleSlug.BILLING_ADMIN: frozenset(
        {
            Permission.TENANT_READ,
            Permission.BILLING_MANAGE,
        }
    ),
    RoleSlug.VIEWER: frozenset(
        {
            Permission.TENANT_READ,
            Permission.CONVERSATIONS_READ,
        }
    ),
}

ROLE_DESCRIPTIONS: Final[Mapping[RoleSlug, str]] = {
    RoleSlug.OWNER: "Full control of the tenant, including billing and ownership.",
    RoleSlug.ADMIN: "Full control of the tenant except billing.",
    RoleSlug.MANAGER: "Runs conversations, catalog, knowledge and AI configuration.",
    RoleSlug.AGENT: "Handles customer conversations.",
    RoleSlug.BILLING_ADMIN: "Manages subscription and billing only.",
    RoleSlug.VIEWER: "Read-only access.",
}

PERMISSION_DESCRIPTIONS: Final[Mapping[Permission, str]] = {
    Permission.TENANT_READ: "View tenant profile and settings.",
    Permission.TENANT_UPDATE: "Change tenant profile and settings.",
    Permission.MEMBERS_INVITE: "Invite people to the tenant.",
    Permission.MEMBERS_MANAGE: "Change or remove memberships and role assignments.",
    Permission.CHANNELS_CONNECT: "Connect a messaging channel.",
    Permission.CHANNELS_MANAGE: "Reconfigure or disconnect messaging channels.",
    Permission.CONVERSATIONS_READ: "Read customer conversations.",
    Permission.CONVERSATIONS_REPLY: "Reply to customer conversations.",
    Permission.CONVERSATIONS_ASSIGN: "Assign conversations to teammates.",
    Permission.AI_CONFIGURE: "Configure AI behaviour.",
    Permission.KNOWLEDGE_MANAGE: "Manage the knowledge base.",
    Permission.CATALOG_MANAGE: "Manage the product catalog.",
    Permission.BILLING_MANAGE: "Manage subscription, payment method and invoices.",
    Permission.APIKEYS_MANAGE: "Create, rotate and revoke API keys.",
    Permission.AUDIT_READ: "Read the tenant audit log.",
}


def permissions_for_roles(role_slugs: Iterable[str]) -> frozenset[Permission]:
    """Return the union of the grants of every recognised role.

    Unknown slugs contribute nothing rather than raising. A role that was
    renamed or removed must fail closed - silently granting nothing - instead
    of turning every request from an affected member into a 500.
    """
    granted: set[Permission] = set()
    for slug in role_slugs:
        try:
            role = RoleSlug(slug)
        except ValueError:
            continue
        granted |= DEFAULT_ROLE_GRANTS[role]
    return frozenset(granted)


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The trusted tenant boundary of the current unit of work.

    Only ever constructed from server-side state - an authenticated session or
    an API key - never from a request body, query parameter or header. Every
    tenant-scoped repository requires one, so "forgot the tenant filter" is not
    a mistake the code allows you to express.
    """

    tenant_id: UUID


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor, resolved once per request.

    `permissions` is already the effective set for `tenant_id`. Callers never
    re-derive it from roles, because two code paths deriving the same answer is
    how they end up disagreeing.
    """

    kind: PrincipalKind
    tenant_id: UUID
    permissions: frozenset[Permission]
    role_slugs: frozenset[str]
    user_id: UUID | None = None
    session_id: UUID | None = None
    membership_id: UUID | None = None
    api_key_id: UUID | None = None

    @property
    def tenant(self) -> TenantContext:
        """Return the tenant boundary this principal is confined to."""
        return TenantContext(tenant_id=self.tenant_id)

    def has_permission(self, permission: Permission) -> bool:
        """Return True when the principal holds `permission` in its tenant."""
        return permission in self.permissions

    def has_all(self, permissions: Iterable[Permission]) -> bool:
        """Return True when the principal holds every listed permission."""
        return all(permission in self.permissions for permission in permissions)


def normalize_email(raw: str) -> str:
    """Return the canonical stored form of an email address.

    Case folding happens here, once, because `users.email` is a plain `text`
    column with a unique constraint rather than `citext` (ADR-0015). If two
    call sites disagreed about normalisation, the database would happily store
    `Ada@example.com` and `ada@example.com` as two accounts.

    Only the domain is truly case-insensitive per RFC 5321, but every provider
    that matters treats the local part that way too, and letting `Ada@` and
    `ada@` be different accounts is an account-takeover vector, not a feature.
    """
    candidate = raw.strip().lower()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        raise ValueError("email address has an unsupported length")
    if not _EMAIL_PATTERN.fullmatch(candidate):
        raise ValueError("email address is not well formed")
    return candidate


def normalize_tenant_slug(raw: str) -> str:
    """Return the canonical URL-safe slug for a tenant."""
    candidate = raw.strip().lower()
    if not MIN_SLUG_LENGTH <= len(candidate) <= MAX_SLUG_LENGTH:
        raise ValueError("tenant slug must be between 3 and 50 characters")
    if not _SLUG_PATTERN.fullmatch(candidate):
        raise ValueError("tenant slug may contain only lowercase letters, digits and hyphens")
    return candidate


def normalize_display_name(raw: str) -> str:
    """Return a trimmed display name, rejecting empty and oversized values."""
    candidate = " ".join(raw.split())
    if not candidate or len(candidate) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError("display name has an unsupported length")
    return candidate


def check_password_policy(password: str, *, min_length: int, max_length: int) -> None:
    """Raise `ValueError` when a candidate password fails the policy.

    Length is the only rule. Composition rules ("one digit, one symbol") shrink
    the search space an attacker has to cover and push people towards
    `Password1!`, so `docs/security.md` 2 specifies a length floor instead. The
    upper bound exists because Argon2id will happily spend CPU hashing a
    multi-megabyte body, which is a free denial-of-service otherwise.
    """
    if len(password) < min_length:
        raise ValueError(f"password must be at least {min_length} characters")
    if len(password) > max_length:
        raise ValueError(f"password must be at most {max_length} characters")
    if password.strip() != password:
        raise ValueError("password must not begin or end with whitespace")
