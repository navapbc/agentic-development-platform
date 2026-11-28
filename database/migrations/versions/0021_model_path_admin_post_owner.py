"""model_path_health column for the posting service's owner token of an episode's admin post

Revision ID: 0021_model_path_admin_post_owner
Revises: 0020_model_path_alert_posts
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_model_path_admin_post_owner"
down_revision: str | None = "0020_model_path_alert_posts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"
TABLE = "model_path_health"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("admin_slack_token", sa.String(64), server_default="", nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(TABLE, "admin_slack_token", schema=SCHEMA)
