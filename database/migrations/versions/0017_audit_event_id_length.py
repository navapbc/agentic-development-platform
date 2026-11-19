"""audit_events event_id widened for long gate lists and decision events

Revision ID: 0017_audit_event_id_length
Revises: 0016_audit_event_remedy
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_audit_event_id_length"
down_revision: str | None = "0016_audit_event_remedy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.alter_column(
        "audit_events",
        "event_id",
        existing_type=sa.String(64),
        type_=sa.String(255),
        existing_nullable=False,
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.alter_column(
        "audit_events",
        "event_id",
        existing_type=sa.String(255),
        type_=sa.String(64),
        existing_nullable=False,
        schema=SCHEMA,
    )
