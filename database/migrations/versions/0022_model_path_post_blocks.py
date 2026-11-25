"""model_path_health columns for the blocks of an episode's tenant post and admin post

Revision ID: 0022_model_path_post_blocks
Revises: 0021_model_path_admin_post_owner
Create Date: 2026-11-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_model_path_post_blocks"
down_revision: str | None = "0021_model_path_admin_post_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"
TABLE = "model_path_health"
COLUMNS = ("slack_blocks", "admin_slack_blocks")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column(
            TABLE, sa.Column(column, sa.Text(), server_default="", nullable=False), schema=SCHEMA
        )


def downgrade() -> None:
    for column in reversed(COLUMNS):
        op.drop_column(TABLE, column, schema=SCHEMA)
