"""usage_metrics table for per-execution token and cost attribution

Revision ID: 0006_usage_metrics
Revises: 0005_suspended_flows
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_usage_metrics"
down_revision: str | None = "0005_suspended_flows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.create_table(
        "usage_metrics",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("stage", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("elapsed_seconds", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_usage_metrics_task_id",
        "usage_metrics",
        ["task_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_metrics_task_id", table_name="usage_metrics", schema=SCHEMA)
    op.drop_table("usage_metrics", schema=SCHEMA)
