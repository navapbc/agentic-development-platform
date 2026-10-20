"""add AWAITING_HUMAN to the execution_state enum

Revision ID: 0007_awaiting_human_state
Revises: 0006_usage_metrics
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_awaiting_human_state"
down_revision: str | None = "0006_usage_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE agent_platform.execution_state ADD VALUE IF NOT EXISTS 'AWAITING_HUMAN' "
        "AFTER 'REPAIRING'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing a value from an enum type.
    raise NotImplementedError("cannot remove AWAITING_HUMAN from the execution_state enum")
