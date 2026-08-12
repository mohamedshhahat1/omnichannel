"""Revision 0002 must stay self-contained and must not drift from the domain.

The migration deliberately duplicates the permission catalogue and the role
grants instead of importing them, because a migration has to keep producing the
same schema years after the application code has moved on. Duplication without
a test is just drift waiting to happen, so the drift check lives here.
"""

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.modules.identity.domain import (
    DEFAULT_ROLE_GRANTS,
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    Permission,
    RoleSlug,
)

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "alembic" / "versions"
MIGRATION_PATH = VERSIONS / "20260812_0100_0002_identity_and_access.py"

_CONSTRAINT_CALLS = frozenset(
    {
        "CheckConstraint",
        "ForeignKeyConstraint",
        "PrimaryKeyConstraint",
        "UniqueConstraint",
    }
)

_EXPECTED_TABLES = frozenset(
    {
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
    }
)


def _load() -> ModuleType:
    """Import the revision file directly; `versions/` is not a package."""
    spec = importlib.util.spec_from_file_location("identity_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load()
SOURCE = MIGRATION_PATH.read_text()
TREE = ast.parse(SOURCE)


def _calls(attribute: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


def _first_string_argument(call: ast.Call) -> str:
    argument = call.args[0]
    assert isinstance(argument, ast.Constant)
    assert isinstance(argument.value, str)
    return argument.value


def test_revision_chain_follows_the_infrastructure_migration() -> None:
    assert MIGRATION.revision == "0002_identity_access"
    assert MIGRATION.down_revision == "0001_initial_infra"


def test_exactly_one_revision_declares_no_parent() -> None:
    # Two roots, or a fork, means `alembic upgrade head` becomes ambiguous.
    roots = [
        path
        for path in VERSIONS.glob("*.py")
        if "down_revision: str | None = None" in path.read_text()
    ]
    assert len(roots) == 1


def test_migration_does_not_import_application_code() -> None:
    # A migration that imports `app.` is a migration that breaks the day a
    # constant is renamed, months after it was applied everywhere.
    assert "from app." not in SOURCE
    assert "import app" not in SOURCE


def test_permission_catalogue_matches_the_domain() -> None:
    assert {slug for slug, _ in MIGRATION.PERMISSIONS} == {item.value for item in Permission}


def test_permission_descriptions_match_the_domain() -> None:
    seeded = dict(MIGRATION.PERMISSIONS)
    expected = {item.value: text for item, text in PERMISSION_DESCRIPTIONS.items()}
    assert seeded == expected


def test_role_catalogue_matches_the_domain() -> None:
    assert {slug for slug, _, _ in MIGRATION.ROLES} == {role.value for role in RoleSlug}


def test_role_descriptions_match_the_domain() -> None:
    seeded = {slug: description for slug, _, description in MIGRATION.ROLES}
    expected = {role.value: text for role, text in ROLE_DESCRIPTIONS.items()}
    assert seeded == expected


@pytest.mark.parametrize("role", list(RoleSlug))
def test_seeded_grants_match_the_domain(role: RoleSlug) -> None:
    seeded = set(MIGRATION.ROLE_GRANTS[role.value])
    expected = {permission.value for permission in DEFAULT_ROLE_GRANTS[role]}
    assert seeded == expected


def test_seeded_grants_cover_every_role_and_nothing_else() -> None:
    assert set(MIGRATION.ROLE_GRANTS) == {role.value for role in RoleSlug}


def test_seeded_grants_only_reference_real_permissions() -> None:
    catalogue = {item.value for item in Permission}
    for granted in MIGRATION.ROLE_GRANTS.values():
        assert set(granted) <= catalogue


def test_creates_every_expected_table() -> None:
    created = {_first_string_argument(call) for call in _calls("create_table")}
    assert created == _EXPECTED_TABLES


def test_downgrade_drops_every_table_it_created() -> None:
    assert set(MIGRATION._TABLES_IN_DROP_ORDER) == _EXPECTED_TABLES
    # Reverse dependency order: children before the parents they reference.
    order = list(MIGRATION._TABLES_IN_DROP_ORDER)
    assert order.index("memberships") < order.index("tenants")
    assert order.index("memberships") < order.index("users")
    assert order.index("membership_roles") < order.index("memberships")
    assert order.index("role_permissions") < order.index("roles")
    assert order.index("audit_logs") < order.index("api_keys")
    assert order.index("sessions") < order.index("users")


def test_every_constraint_is_explicitly_named() -> None:
    # Alembic builds its own MetaData for op.create_table, so the naming
    # convention on Base does not apply. Anything unnamed here would get a
    # PostgreSQL default name and disagree with the models forever.
    calls = [call for name in _CONSTRAINT_CALLS for call in _calls(name)]
    assert calls
    for call in calls:
        assert "name" in {keyword.arg for keyword in call.keywords}


def test_constraint_names_follow_the_project_convention() -> None:
    suffixes = {
        "PrimaryKeyConstraint": "_pk",
        "UniqueConstraint": "_uq",
        "CheckConstraint": "_ck",
    }
    for call_name, suffix in suffixes.items():
        for call in _calls(call_name):
            name = next(kw for kw in call.keywords if kw.arg == "name")
            assert isinstance(name.value, ast.Constant)
            assert str(name.value.value).endswith(suffix)

    for call in _calls("ForeignKeyConstraint"):
        name = next(kw for kw in call.keywords if kw.arg == "name")
        assert isinstance(name.value, ast.Constant)
        assert str(name.value.value).startswith("fk_")


def test_every_index_name_ends_with_idx() -> None:
    for call in _calls("create_index"):
        assert _first_string_argument(call).endswith("_idx")


def test_seed_statements_use_bound_parameters() -> None:
    # Seed values are literals today, but an INSERT built by string formatting
    # is the template someone copies later with a variable in it.
    assert "bindparams" in SOURCE
    for node in ast.walk(TREE):
        if isinstance(node, ast.JoinedStr):
            pytest.fail("revision 0002 must not build SQL with f-strings")


def test_credentials_are_stored_as_digests_only() -> None:
    for column in ("password_hash", "token_digest", "csrf_digest", "secret_digest"):
        assert column in SOURCE
    # Nothing in the schema may be named as if it holds recoverable material.
    for forbidden in ('"password"', '"secret"', '"api_key"', '"token"'):
        assert forbidden not in SOURCE
