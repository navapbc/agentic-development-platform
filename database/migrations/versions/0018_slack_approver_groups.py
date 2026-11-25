"""slack_approver_groups store of each tenant's Slack user group members and their read time

Revision ID: 0018_slack_approver_groups
Revises: 0017_audit_event_id_length
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_slack_approver_groups"
down_revision: str | None = "0017_audit_event_id_length"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "slack_approver_groups",
        sa.Column("tenant", sa.String(32), nullable=False),
        sa.Column("group_id", sa.String(32), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "group_id", name="pk_slack_approver_groups"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("slack_approver_groups", schema=SCHEMA)
