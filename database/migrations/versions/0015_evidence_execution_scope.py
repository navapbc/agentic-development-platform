"""evidence deduplicated per execution, and the tenant of each evidence artifact

Revision ID: 0015_evidence_execution_scope
Revises: 0014_claim_tenant
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_evidence_execution_scope"
down_revision: str | None = "0014_claim_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"

_CONTENT_UNIQUE = "uq_evidence_artifacts_content_hash"
_EXECUTION_CONTENT_UNIQUE = "uq_evidence_artifacts_execution_content_hash"
_TENANT_INDEX = "ix_evidence_artifacts_tenant"


def upgrade() -> None:
    op.add_column(
        "evidence_artifacts", sa.Column("tenant", sa.String(32), nullable=True), schema=SCHEMA
    )
    op.execute(
        "UPDATE agent_platform.evidence_artifacts AS artifact "
        "SET tenant = COALESCE(execution.tenant, '') "
        "FROM agent_platform.executions AS execution "
        "WHERE execution.execution_id = artifact.execution_id"
    )
    op.execute("UPDATE agent_platform.evidence_artifacts SET tenant = '' WHERE tenant IS NULL")
    op.alter_column(
        "evidence_artifacts",
        "tenant",
        existing_type=sa.String(32),
        nullable=False,
        server_default="",
        schema=SCHEMA,
    )
    op.create_index(op.f(_TENANT_INDEX), "evidence_artifacts", ["tenant"], schema=SCHEMA)
    op.drop_constraint(op.f(_CONTENT_UNIQUE), "evidence_artifacts", type_="unique", schema=SCHEMA)
    op.create_unique_constraint(
        op.f(_EXECUTION_CONTENT_UNIQUE),
        "evidence_artifacts",
        ["execution_id", "content_hash"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f(_EXECUTION_CONTENT_UNIQUE), "evidence_artifacts", type_="unique", schema=SCHEMA
    )
    op.execute(
        "DELETE FROM agent_platform.evidence_artifacts AS artifact WHERE EXISTS "
        "(SELECT 1 FROM agent_platform.evidence_artifacts AS kept "
        "WHERE kept.content_hash = artifact.content_hash "
        "AND (kept.created_at, kept.artifact_id) < (artifact.created_at, artifact.artifact_id))"
    )
    op.create_unique_constraint(
        op.f(_CONTENT_UNIQUE), "evidence_artifacts", ["content_hash"], schema=SCHEMA
    )
    op.drop_index(op.f(_TENANT_INDEX), table_name="evidence_artifacts", schema=SCHEMA)
    op.drop_column("evidence_artifacts", "tenant", schema=SCHEMA)
