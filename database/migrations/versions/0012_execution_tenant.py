"""tenant of each execution, which scopes admission and reaping to the tenant's worker

Revision ID: 0012_execution_tenant
Revises: 0011_run_file_impact
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_execution_tenant"
down_revision: str | None = "0011_run_file_impact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.add_column("executions", sa.Column("tenant", sa.String(32), nullable=True), schema=SCHEMA)
    op.create_index("ix_executions_tenant", "executions", ["tenant"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_executions_tenant", table_name="executions", schema=SCHEMA)
    op.drop_column("executions", "tenant", schema=SCHEMA)
