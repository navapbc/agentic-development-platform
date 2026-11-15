"""tenant of each orphan artifact, which scopes orphan ids and reaping to the tenant's worker

Revision ID: 0013_orphan_tenant
Revises: 0012_execution_tenant
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_orphan_tenant"
down_revision: str | None = "0012_execution_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.add_column(
        "orphan_artifacts",
        sa.Column("tenant", sa.String(32), nullable=False, server_default=""),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("orphan_artifacts", "tenant", schema=SCHEMA)
