"""limit_grants table for the limit increases a tenant requests and an administrator decides

Revision ID: 0023_limit_grants
Revises: 0022_model_path_post_blocks
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_limit_grants"
down_revision: str | None = "0022_model_path_post_blocks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"
TABLE = "limit_grants"
INDEX = "ix_limit_grants_tenant_limit_name"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("tenant", sa.String(32), nullable=False),
        sa.Column("limit_name", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("execution_id", sa.String(64), server_default="", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("requester", sa.String(128), nullable=False),
        sa.Column("approver", sa.String(128), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admin_slack_ts", sa.String(32), server_default="", nullable=False),
        sa.Column("admin_slack_token", sa.String(64), server_default="", nullable=False),
        sa.Column("admin_slack_blocks", sa.Text(), server_default="", nullable=False),
        sa.Column("tenant_slack_channel", sa.String(32), server_default="", nullable=False),
        sa.Column("tenant_slack_ts", sa.String(32), server_default="", nullable=False),
        sa.Column("tenant_slack_blocks", sa.Text(), server_default="", nullable=False),
        schema=SCHEMA,
    )
    op.create_index(INDEX, TABLE, ["tenant", "limit_name"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE, schema=SCHEMA)
    op.drop_table(TABLE, schema=SCHEMA)
