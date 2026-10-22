"""rename CI_RUNNING to CHECKS_RUNNING in the execution_state enum

Revision ID: 0009_checks_running_state
Revises: 0008_review_state
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_checks_running_state"
down_revision: str | None = "0008_review_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE agent_platform.execution_state RENAME VALUE 'CI_RUNNING' TO 'CHECKS_RUNNING'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TYPE agent_platform.execution_state RENAME VALUE 'CHECKS_RUNNING' TO 'CI_RUNNING'"
    )
