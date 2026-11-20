"""release work_claims that predate ownership tracking

Revision ID: 0004_release_preownership_claims
Revises: 0003_work_claim_owner
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_release_preownership_claims"
down_revision: str | None = "0003_work_claim_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_platform.work_claims "
        "SET state = 'released', released_at = now() "
        "WHERE state = 'claimed' AND owner_workflow_id IS NULL"
    )


def downgrade() -> None:
    pass
