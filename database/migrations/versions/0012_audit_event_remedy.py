"""audit_events remedy and alert_channels for operator next steps

Revision ID: 0012_audit_event_remedy
Revises: 0011_run_file_impact
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_audit_event_remedy"
down_revision: str | None = "0011_run_file_impact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("remedy", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "audit_events",
        sa.Column("alert_channels", sa.JSON(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("audit_events", "alert_channels", schema=SCHEMA)
    op.drop_column("audit_events", "remedy", schema=SCHEMA)
