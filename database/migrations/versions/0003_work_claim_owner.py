"""work_claims owner workflow id for liveness takeover

Revision ID: 0003_work_claim_owner
Revises: 0002_side_effects
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_work_claim_owner"
down_revision: str | None = "0002_side_effects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.add_column(
        "work_claims",
        sa.Column("owner_workflow_id", sa.String(255), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("work_claims", "owner_workflow_id", schema=SCHEMA)
