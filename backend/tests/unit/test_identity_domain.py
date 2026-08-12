"""The RBAC tables are a security control, so they are asserted literally.

`docs/security.md` 3 is the source of truth for both the permission catalogue
and the role grants. This module restates that table independently of
`app.modules.identity.domain` rather than importing it and comparing it to
itself. The point is that widening a role cannot pass review quietly: the
expectation below has to change in the same diff, where a reviewer will see it.
"""

import uuid

import pytest

from app.modules.identity.domain import (
    DEFAULT_ROLE_GRANTS,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_SLUG_LENGTH,
    MIN_SLUG_LENGTH,
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    Permission,
    Principal,
    PrincipalKind,
    RoleSlug,
    check_password_policy,
    normalize_display_name,
    normalize_email,
    normalize_tenant_slug,
    permissions_for_roles,
)

_CATALOGUE: set[str] = {
    "tenant.read",
    "tenant.update",
    "members.invite",
    "members.manage",
    "channels.connect",
    "channels.manage",
    "conversations.read",
    "conversations.reply",
    "conversations.assign",
    "ai.configure",
    "knowledge.manage",
    "catalog.manage",
    "billing.manage",
    "apikeys.manage",
    "audit.read",
}

_EXPECTED_GRANTS: dict[str, set[str]] = {
    "owner": set(_CATALOGUE),
    "admin": _CATALOGUE - {"billing.manage"},
    "manager": {
        "tenant.read",
        "conversations.read",
        "conversations.reply",
        "conversations.assign",
        "catalog.manage",
        "knowledge.manage",
        "ai.configure",
    },
    "agent": {
        "tenant.read",
        "conversations.read",
        "conversations.reply",
        "conversations.assign",
    },
    "billing_admin": {"tenant.read", "billing.manage"},
    "viewer": {"tenant.read", "conversations.read"},
}


def _granted(role: RoleSlug) -> set[str]:
    return {permission.value for permission in DEFAULT_ROLE_GRANTS[role]}


def test_permission_catalogue_is_exactly_the_documented_set() -> None:
    assert {permission.value for permission in Permission} == _CATALOGUE


def test_role_catalogue_is_exactly_the_six_system_roles() -> None:
    assert {role.value for role in RoleSlug} == set(_EXPECTED_GRANTS)


@pytest.mark.parametrize("role", list(RoleSlug))
def test_role_grants_match_the_documented_table(role: RoleSlug) -> None:
    assert _granted(role) == _EXPECTED_GRANTS[role.value]


def test_owner_is_the_only_role_that_can_manage_billing() -> None:
    billing = {
        role.value
        for role, permissions in DEFAULT_ROLE_GRANTS.items()
        if Permission.BILLING_MANAGE in permissions
    }
    assert billing == {"owner", "billing_admin"}


def test_admin_is_owner_minus_billing() -> None:
    # The single difference between the two most powerful roles. If this ever
    # becomes an empty difference, "Admin" silently became "Owner".
    assert _granted(RoleSlug.OWNER) - _granted(RoleSlug.ADMIN) == {"billing.manage"}


def test_viewer_cannot_write_anything() -> None:
    writes = {
        "tenant.update",
        "members.invite",
        "members.manage",
        "channels.connect",
        "channels.manage",
        "conversations.reply",
        "conversations.assign",
        "ai.configure",
        "knowledge.manage",
        "catalog.manage",
        "billing.manage",
        "apikeys.manage",
    }
    assert _granted(RoleSlug.VIEWER) & writes == set()


def test_only_owner_and_admin_can_manage_api_keys() -> None:
    holders = {
        role.value
        for role, permissions in DEFAULT_ROLE_GRANTS.items()
        if Permission.APIKEYS_MANAGE in permissions
    }
    assert holders == {"owner", "admin"}


def test_every_permission_and_role_is_described() -> None:
    assert set(PERMISSION_DESCRIPTIONS) == set(Permission)
    assert set(ROLE_DESCRIPTIONS) == set(RoleSlug)
    assert all(text.strip() for text in PERMISSION_DESCRIPTIONS.values())
    assert all(text.strip() for text in ROLE_DESCRIPTIONS.values())


def test_permissions_for_roles_unions_every_grant() -> None:
    granted = permissions_for_roles(["agent", "billing_admin"])
    assert {permission.value for permission in granted} == {
        "tenant.read",
        "conversations.read",
        "conversations.reply",
        "conversations.assign",
        "billing.manage",
    }


def test_permissions_for_roles_is_empty_without_roles() -> None:
    assert permissions_for_roles([]) == frozenset()


@pytest.mark.parametrize("slug", ["superuser", "OWNER", "owner ", "", "admin;--"])
def test_unknown_role_slugs_grant_nothing(slug: str) -> None:
    # Fails closed. A renamed or deleted role must strip permissions, never
    # raise into a 500 and never match by accident through case folding.
    assert permissions_for_roles([slug]) == frozenset()


def test_unknown_role_does_not_poison_a_valid_one() -> None:
    assert permissions_for_roles(["superuser", "viewer"]) == permissions_for_roles(["viewer"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ada@Example.COM", "ada@example.com"),
        ("  ada@example.com  ", "ada@example.com"),
        ("ADA+tag@example.com", "ada+tag@example.com"),
    ],
)
def test_normalize_email_folds_case_and_trims(raw: str, expected: str) -> None:
    # Case folding is an account-takeover control, not a nicety: `users.email`
    # is a plain unique text column, so `Ada@` and `ada@` would otherwise be
    # two separate accounts for the same mailbox.
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "no-at-sign", "two@@example.com", "trailing@", "@example.com", "a b@example.com"],
)
def test_normalize_email_rejects_malformed_addresses(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_normalize_email_rejects_oversized_addresses() -> None:
    oversized = ("a" * MAX_EMAIL_LENGTH) + "@example.com"
    with pytest.raises(ValueError):
        normalize_email(oversized)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Acme", "acme"), ("  acme-co  ", "acme-co"), ("ACME2", "acme2")],
)
def test_normalize_tenant_slug_folds_case(raw: str, expected: str) -> None:
    assert normalize_tenant_slug(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "ab", "has space", "under_score", "trailing-", "-leading", "dots.here", "sl/ash"],
)
def test_normalize_tenant_slug_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_tenant_slug(raw)


def test_normalize_tenant_slug_enforces_both_bounds() -> None:
    assert len(normalize_tenant_slug("a" * MIN_SLUG_LENGTH)) == MIN_SLUG_LENGTH
    assert len(normalize_tenant_slug("a" * MAX_SLUG_LENGTH)) == MAX_SLUG_LENGTH
    with pytest.raises(ValueError):
        normalize_tenant_slug("a" * (MAX_SLUG_LENGTH + 1))


def test_normalize_display_name_collapses_whitespace() -> None:
    assert normalize_display_name("  Ada   Lovelace ") == "Ada Lovelace"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_normalize_display_name_rejects_empty(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_display_name(raw)


def test_normalize_display_name_rejects_oversized() -> None:
    with pytest.raises(ValueError):
        normalize_display_name("a" * (MAX_DISPLAY_NAME_LENGTH + 1))


def test_password_policy_accepts_a_long_passphrase() -> None:
    check_password_policy("correct horse battery staple", min_length=12, max_length=1024)


@pytest.mark.parametrize(
    "candidate",
    ["short", "", " leading space is bad ", "trailing "],
)
def test_password_policy_rejects_bad_candidates(candidate: str) -> None:
    with pytest.raises(ValueError):
        check_password_policy(candidate, min_length=12, max_length=1024)


def test_password_policy_caps_length_to_bound_hashing_cost() -> None:
    # Argon2id will happily burn CPU on a multi-megabyte body. The cap is a
    # denial-of-service control.
    with pytest.raises(ValueError):
        check_password_policy("a" * 1025, min_length=12, max_length=1024)


def test_password_policy_error_never_echoes_the_password() -> None:
    secret = "hunter2"
    with pytest.raises(ValueError) as caught:
        check_password_policy(secret, min_length=12, max_length=1024)
    assert secret not in str(caught.value)


def _principal(*, permissions: frozenset[Permission], tenant_id: uuid.UUID) -> Principal:
    return Principal(
        kind=PrincipalKind.USER,
        tenant_id=tenant_id,
        permissions=permissions,
        role_slugs=frozenset({"agent"}),
        user_id=uuid.uuid4(),
    )


def test_principal_reports_the_permissions_it_holds() -> None:
    tenant_id = uuid.uuid4()
    principal = _principal(
        permissions=frozenset({Permission.CONVERSATIONS_READ}),
        tenant_id=tenant_id,
    )
    assert principal.has_permission(Permission.CONVERSATIONS_READ)
    assert not principal.has_permission(Permission.BILLING_MANAGE)
    assert principal.has_all([Permission.CONVERSATIONS_READ])
    assert not principal.has_all([Permission.CONVERSATIONS_READ, Permission.BILLING_MANAGE])
    assert principal.tenant.tenant_id == tenant_id


def test_principal_is_immutable() -> None:
    # A request handler must not be able to widen its own authority mid-request.
    principal = _principal(permissions=frozenset(), tenant_id=uuid.uuid4())
    with pytest.raises((AttributeError, TypeError)):
        principal.permissions = frozenset(Permission)  # type: ignore[misc]
