"""Initial infrastructure extensions; intentionally creates no domain tables.

Revision ID: 0001_initial_infra
Revises: None
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_infra"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXTENSIONS = ("pgcrypto", "pg_stat_statements", "pg_trgm", "vector")


def upgrade() -> None:
    for extension in _EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')


def downgrade() -> None:
    for extension in reversed(_EXTENSIONS):
        op.execute(f'DROP EXTENSION IF EXISTS "{extension}"')
