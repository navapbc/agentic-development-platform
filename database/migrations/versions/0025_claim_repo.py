"""repository of each work claim, which scopes the file paths a claim holds to that repository

An empty value records no repository, so a claim written before this revision holds its paths in
every repository of its tenant until it is released.

Revision ID: 0025_claim_repo
Revises: 0024_tenant_pause
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_claim_repo"
down_revision: str | None = "0024_tenant_pause"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"


def upgrade() -> None:
    op.add_column(
        "work_claims",
        sa.Column("repo", sa.String(255), nullable=False, server_default=""),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("work_claims", "repo", schema=SCHEMA)
