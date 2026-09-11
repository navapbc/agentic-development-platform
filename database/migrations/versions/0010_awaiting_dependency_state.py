"""add AWAITING_DEPENDENCY to the execution_state enum

Revision ID: 0010_awaiting_dependency_state
Revises: 0009_checks_running_state
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_awaiting_dependency_state"
down_revision: str | None = "0009_checks_running_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.execute(
        f"ALTER TYPE {SCHEMA}.execution_state ADD VALUE IF NOT EXISTS 'AWAITING_DEPENDENCY' "
        "AFTER 'AWAITING_PR_CLOSE'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing a value from an enum type.
    raise NotImplementedError(
        "cannot remove AWAITING_DEPENDENCY from the execution_state enum"
    )
