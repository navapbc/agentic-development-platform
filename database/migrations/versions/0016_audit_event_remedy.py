"""audit_events remedy and alert_channels for operator next steps

Revision ID: 0016_audit_event_remedy
Revises: 0015_evidence_execution_scope
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_audit_event_remedy"
down_revision: str | None = "0015_evidence_execution_scope"
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
