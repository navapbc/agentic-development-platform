"""tenant_pauses table for the Park all jobs pause a tenant sets and Resume jobs clears

Revision ID: 0024_tenant_pause
Revises: 0023_limit_grants
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_tenant_pause"
down_revision: str | None = "0023_limit_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"
TABLE = "tenant_pauses"
INDEX = "uq_tenant_pauses_active_tenant"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("pause_id", sa.String(64), primary_key=True),
        sa.Column("tenant", sa.String(32), nullable=False),
        sa.Column("paused_by", sa.String(128), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_by", sa.String(128), server_default="", nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        INDEX,
        TABLE,
        ["tenant"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("cleared_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
