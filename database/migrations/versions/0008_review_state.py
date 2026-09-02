"""add REVIEW to the execution_state enum

Revision ID: 0008_review_state
Revises: 0007_awaiting_human_state
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_review_state"
down_revision: str | None = "0007_awaiting_human_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.execute(
        f"ALTER TYPE {SCHEMA}.execution_state ADD VALUE IF NOT EXISTS 'REVIEW' "
        "BEFORE 'CODE_REVIEW'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing a value from an enum type.
    raise NotImplementedError("cannot remove REVIEW from the execution_state enum")
