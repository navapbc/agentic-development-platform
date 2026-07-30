"""side_effects idempotency ledger

Revision ID: 0002_side_effects
Revises: 0001_initial
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_side_effects"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "side_effects",
        sa.Column("side_effect_key", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_side_effects_task_id", "side_effects", ["task_id"], schema=SCHEMA)
    op.create_index("ix_side_effects_execution_id", "side_effects", ["execution_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_side_effects_execution_id", "side_effects", schema=SCHEMA)
    op.drop_index("ix_side_effects_task_id", "side_effects", schema=SCHEMA)
    op.drop_table("side_effects", schema=SCHEMA)
