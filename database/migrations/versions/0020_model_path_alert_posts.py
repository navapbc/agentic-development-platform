"""model_path_health columns for the Slack posts of an episode, its acknowledger, its resolver,
and the host agent's last action

Revision ID: 0020_model_path_alert_posts
Revises: 0019_model_path_health
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_model_path_alert_posts"
down_revision: str | None = "0019_model_path_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"
TABLE = "model_path_health"

TEXT_COLUMNS = (
    ("slack_channel", 32),
    ("slack_ts", 32),
    ("admin_slack_channel", 32),
    ("admin_slack_ts", 32),
    ("acknowledged_by", 128),
    ("resolved_by", 128),
    ("host_action", 500),
)
TIME_COLUMNS = ("acknowledged_at", "resolved_at", "host_action_at", "host_action_posted_at")


def upgrade() -> None:
    for name, length in TEXT_COLUMNS:
        op.add_column(
            TABLE,
            sa.Column(name, sa.String(length), server_default="", nullable=False),
            schema=SCHEMA,
        )
    for name in TIME_COLUMNS:
        op.add_column(TABLE, sa.Column(name, sa.DateTime(timezone=True)), schema=SCHEMA)


def downgrade() -> None:
    for name in reversed(TIME_COLUMNS):
        op.drop_column(TABLE, name, schema=SCHEMA)
    for name, _ in reversed(TEXT_COLUMNS):
        op.drop_column(TABLE, name, schema=SCHEMA)
