"""Async database construction is lazy and uses approved metadata policy."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import Base, create_database_engine, create_session_factory, session_scope
from app.core.settings import DatabaseSettings
from app.modules.identity import models  # noqa: F401


def test_engine_is_async_and_does_not_connect_during_construction() -> None:
    engine = create_database_engine(DatabaseSettings())
    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.pool is not None
    finally:
        asyncio.run(engine.dispose())


def test_session_factory_is_bound_to_engine() -> None:
    engine = create_database_engine(DatabaseSettings())
    try:
        factory = create_session_factory(engine)
        assert factory.kw["bind"] is engine
        assert factory.kw["expire_on_commit"] is False
    finally:
        asyncio.run(engine.dispose())


def test_metadata_has_stable_constraint_names_and_registers_identity_tables() -> None:
    # Importing app.modules.identity.models (top of file) registers the
    # module's tables on the shared Base metadata, which is what Alembic
    # autogenerate diffs against. Phase 1 asserted this metadata was empty;
    # Phase 2 populated it, so pin the exact table set instead.
    convention = Base.metadata.naming_convention
    assert convention is not None
    assert convention["pk"] == "%(table_name)s_pk"
    fk = convention["fk"]
    assert isinstance(fk, str)
    assert fk.startswith("fk_")
    assert set(Base.metadata.tables) == {
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
    }


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error() -> None:
    class FakeSession:
        committed = False
        rolled_back = False

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True

    fake = FakeSession()
    with pytest.raises(RuntimeError):
        async with session_scope(lambda: fake):  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert fake.rolled_back is True
    assert fake.committed is False
