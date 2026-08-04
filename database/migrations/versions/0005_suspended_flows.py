"""suspended_flows registry for parked workflows awaiting resume

Revision ID: 0005_suspended_flows
Revises: 0004_release_preownership_claims
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_suspended_flows"
down_revision: str | None = "0004_release_preownership_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "suspended_flows",
        sa.Column("workflow_id", sa.String(255), primary_key=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("resume_on", sa.String(32), nullable=False),
        sa.Column("parked_build_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("stage", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "suspended_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("suspended_flows", schema=SCHEMA)
