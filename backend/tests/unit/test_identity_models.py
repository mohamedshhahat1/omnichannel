"""Schema-level invariants asserted against the SQLAlchemy metadata.

These run without a database. They exist because the expensive schema mistakes
- a missing tenant column, a CASCADE that deletes an audit trail, a naive
timestamp - are all visible in the metadata long before anything is deployed.
"""

import pytest
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Table, UniqueConstraint

from app.core.database import Base
from app.modules.identity import models

IDENTITY_TABLES = (
    "api_keys",
    "audit_logs",
    "email_tokens",
    "membership_roles",
    "memberships",
    "permissions",
    "role_permissions",
    "roles",
    "sessions",
    "tenants",
    "users",
)

# Rows that belong to exactly one customer. `roles` is absent on purpose: a
# NULL tenant_id there marks a shared system role.
TENANT_SCOPED = ("memberships", "api_keys")


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _foreign_key(table: str, column: str) -> ForeignKeyConstraint:
    for constraint in _table(table).foreign_key_constraints:
        if [element.parent.name for element in constraint.elements] == [column]:
            return constraint
    pytest.fail(f"{table}.{column} has no foreign key")


def test_every_identity_table_is_registered() -> None:
    assert set(IDENTITY_TABLES) <= set(Base.metadata.tables)


def test_models_module_exposes_the_mapped_classes() -> None:
    assert models.Tenant.__tablename__ == "tenants"
    assert models.User.__tablename__ == "users"
    assert models.Membership.__tablename__ == "memberships"
    assert models.ApiKey.__tablename__ == "api_keys"
    assert models.UserSession.__tablename__ == "sessions"
    assert models.AuditLog.__tablename__ == "audit_logs"


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_primary_key_follows_the_naming_convention(name: str) -> None:
    assert str(_table(name).primary_key.name) == f"{name}_pk"


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_constraint_names_follow_the_naming_convention(name: str) -> None:
    table = _table(name)
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            assert str(constraint.name).endswith("_uq")
        elif isinstance(constraint, CheckConstraint):
            assert str(constraint.name).endswith("_ck")
        elif isinstance(constraint, ForeignKeyConstraint):
            assert str(constraint.name).startswith("fk_")


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_index_names_follow_the_naming_convention(name: str) -> None:
    for index in _table(name).indexes:
        assert str(index.name).endswith("_idx")


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_identifiers_are_uuid_primary_keys(name: str) -> None:
    for column in _table(name).primary_key.columns:
        assert column.type.python_type.__name__ in {"UUID", "uuid"}


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_every_timestamp_is_timezone_aware(name: str) -> None:
    # A naive timestamp silently adopts the server's local zone, which is how
    # expiry logic ends up off by an hour twice a year.
    for column in _table(name).columns:
        if isinstance(column.type, DateTime):
            assert column.type.timezone is True


@pytest.mark.parametrize("name", IDENTITY_TABLES)
def test_every_table_records_when_the_row_was_created(name: str) -> None:
    created = _table(name).columns["created_at"]
    assert not created.nullable
    assert created.server_default is not None


@pytest.mark.parametrize("name", TENANT_SCOPED)
def test_tenant_scoped_tables_require_a_tenant(name: str) -> None:
    # NOT NULL is what makes "forgot the tenant" a write failure rather than an
    # orphan row that leaks across the boundary later.
    assert not _table(name).columns["tenant_id"].nullable


def test_a_session_may_exist_before_a_tenant_is_chosen() -> None:
    # Someone who has registered but not yet created a workspace still needs a
    # session in order to create one.
    assert _table("sessions").columns["tenant_id"].nullable


def test_system_roles_are_shared_and_tenant_roles_are_not() -> None:
    assert _table("roles").columns["tenant_id"].nullable


def test_no_column_is_named_as_if_it_stored_recoverable_material() -> None:
    forbidden = {"password", "secret", "token", "api_key", "plaintext", "credential"}
    for name in IDENTITY_TABLES:
        for column in _table(name).columns:
            assert column.name not in forbidden


def test_credentials_are_stored_only_as_digests() -> None:
    assert "password_hash" in _table("users").columns
    assert "token_digest" in _table("sessions").columns
    assert "csrf_digest" in _table("sessions").columns
    assert "secret_digest" in _table("api_keys").columns
    assert "token_digest" in _table("email_tokens").columns


def test_opaque_credentials_are_unique_so_lookup_is_a_single_read() -> None:
    def _unique_columns(name: str) -> set[tuple[str, ...]]:
        return {
            tuple(column.name for column in constraint.columns)
            for constraint in _table(name).constraints
            if isinstance(constraint, UniqueConstraint)
        }

    assert ("token_digest",) in _unique_columns("sessions")
    assert ("key_id",) in _unique_columns("api_keys")
    assert ("token_digest",) in _unique_columns("email_tokens")
    assert ("email",) in _unique_columns("users")
    assert ("slug",) in _unique_columns("tenants")
    assert ("tenant_id", "user_id") in _unique_columns("memberships")


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("memberships", "tenant_id"),
        ("memberships", "user_id"),
        ("sessions", "user_id"),
        ("sessions", "tenant_id"),
        ("api_keys", "tenant_id"),
        ("email_tokens", "user_id"),
        ("membership_roles", "membership_id"),
        ("role_permissions", "role_id"),
        ("role_permissions", "permission_id"),
    ],
)
def test_dependent_rows_are_removed_with_their_parent(table: str, column: str) -> None:
    assert _foreign_key(table, column).ondelete == "CASCADE"


def test_an_assigned_role_cannot_be_deleted_out_from_under_a_member() -> None:
    # RESTRICT, not CASCADE: deleting a role must not silently strip the
    # permissions of everyone holding it.
    assert _foreign_key("membership_roles", "role_id").ondelete == "RESTRICT"


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("memberships", "invited_by_id"),
        ("api_keys", "created_by_id"),
        ("audit_logs", "tenant_id"),
        ("audit_logs", "actor_user_id"),
        ("audit_logs", "actor_api_key_id"),
    ],
)
def test_references_that_must_outlive_their_target_are_nulled(table: str, column: str) -> None:
    # An audit row that disappears with the account it describes is not an
    # audit trail.
    assert _foreign_key(table, column).ondelete == "SET NULL"


def test_the_audit_log_is_append_only() -> None:
    assert "updated_at" not in _table("audit_logs").columns


def test_soft_deleted_tables_carry_a_deletion_timestamp() -> None:
    # Tenants and memberships carry history that has to outlive them.
    assert "deleted_at" in _table("tenants").columns
    assert "deleted_at" in _table("memberships").columns


def test_lifecycle_columns_are_constrained_to_known_states() -> None:
    def _checks(name: str) -> str:
        return " ".join(
            str(constraint.sqltext)
            for constraint in _table(name).constraints
            if isinstance(constraint, CheckConstraint)
        )

    assert "active" in _checks("tenants")
    assert "deactivated" in _checks("users")
    assert "invited" in _checks("memberships")
    assert "success" in _checks("audit_logs")


def test_normalisation_is_enforced_by_the_database_not_only_the_application() -> None:
    # `users.email` is a plain unique text column, so the lowercase invariant
    # has to be a constraint. Otherwise two code paths that disagree about
    # normalisation create two accounts for one mailbox.
    checks = " ".join(
        str(constraint.sqltext)
        for constraint in _table("users").constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "lower(email)" in checks
