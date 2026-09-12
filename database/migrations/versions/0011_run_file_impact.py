"""run_file_impact store for per-run predicted touch sets

Revision ID: 0011_run_file_impact
Revises: 0010_awaiting_dependency_state
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_run_file_impact"
down_revision: str | None = "0010_awaiting_dependency_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "run_file_impact",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("run_file_impact", schema=SCHEMA)
