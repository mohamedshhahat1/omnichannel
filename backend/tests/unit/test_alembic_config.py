"""Alembic configuration remains async, settings-driven, and model-free."""

from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).parents[2]
ENV = ROOT / "alembic" / "env.py"
MIGRATION = ROOT / "alembic" / "versions" / "20260811_0000_0001_initial_infrastructure.py"


def test_alembic_ini_parses_and_has_no_real_dsn() -> None:
    # Alembic's own %(here)s interpolation is not configparser-compatible, so
    # the file must be read with configparser interpolation disabled.
    parser = ConfigParser(interpolation=None)
    parser.read(ROOT / "alembic.ini")
    assert parser["alembic"]["script_location"] == "%(here)s/alembic"
    assert parser["alembic"]["sqlalchemy.url"] == "driver://unused"


def test_env_uses_async_engine_and_migration_settings() -> None:
    source = ENV.read_text()
    assert "async_engine_from_config" in source
    assert "database.migration_url" in source
    assert "Base.metadata" in source


def test_initial_migration_only_manages_extensions() -> None:
    source = MIGRATION.read_text()
    for extension in ("pgcrypto", "pg_stat_statements", "pg_trgm", "vector"):
        assert extension in source
    assert "CREATE TABLE" not in source.upper()
    assert "CASCADE" not in source.upper()
    assert "down_revision: str | None = None" in source
