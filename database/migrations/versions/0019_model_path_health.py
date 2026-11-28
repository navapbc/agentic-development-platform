"""model_path_health row per tenant recording an open model path outage episode

Revision ID: 0019_model_path_health
Revises: 0018_slack_approver_groups
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_model_path_health"
down_revision: str | None = "0018_slack_approver_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "model_path_health",
        sa.Column("tenant", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("hop", sa.String(32), server_default="", nullable=False),
        sa.Column("episode", sa.String(64), nullable=False),
        sa.Column(
            "since", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant", name="pk_model_path_health"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("model_path_health", schema=SCHEMA)
