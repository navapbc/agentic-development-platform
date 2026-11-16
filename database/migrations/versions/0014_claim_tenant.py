"""tenant of each work claim and sequence gate, which scopes claims and sequencing to the tenant

Revision ID: 0014_claim_tenant
Revises: 0013_orphan_tenant
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_claim_tenant"
down_revision: str | None = "0013_orphan_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"

_CLAIMS_UNIQUE = "uq_work_claims_task_work_unit"
_TENANT_CLAIMS_UNIQUE = "uq_work_claims_tenant_task_work_unit"
_GATE_PRIMARY_KEY = "pk_sequence_gate"


def _add_nullable_tenant(table: str) -> None:
    op.add_column(table, sa.Column("tenant", sa.String(32), nullable=True), schema=SCHEMA)


def _require_tenant(table: str) -> None:
    op.alter_column(
        table,
        "tenant",
        existing_type=sa.String(32),
        nullable=False,
        server_default="",
        schema=SCHEMA,
    )


def upgrade() -> None:
    _add_nullable_tenant("work_claims")
    op.execute(
        "UPDATE agent_platform.work_claims AS claim "
        "SET tenant = COALESCE(execution.tenant, '') "
        "FROM agent_platform.executions AS execution "
        "WHERE execution.execution_id = claim.execution_id"
    )
    op.execute("UPDATE agent_platform.work_claims SET tenant = '' WHERE tenant IS NULL")
    _require_tenant("work_claims")
    # Matches services.coordination.claims.claim_id_for, which appends a tenant that is set.
    op.execute(
        "UPDATE agent_platform.work_claims "
        "SET claim_id = encode(sha256(convert_to("
        "task_id || chr(31) || work_unit || chr(31) || tenant, 'UTF8')), 'hex') "
        "WHERE tenant <> ''"
    )
    op.drop_constraint(op.f(_CLAIMS_UNIQUE), "work_claims", type_="unique", schema=SCHEMA)
    op.create_unique_constraint(
        op.f(_TENANT_CLAIMS_UNIQUE),
        "work_claims",
        ["tenant", "task_id", "work_unit"],
        schema=SCHEMA,
    )

    _add_nullable_tenant("sequence_gate")
    op.drop_constraint(op.f(_GATE_PRIMARY_KEY), "sequence_gate", type_="primary", schema=SCHEMA)
    op.execute(
        "INSERT INTO agent_platform.sequence_gate "
        "(tenant, task_id, current_sequence, updated_at) "
        "SELECT DISTINCT COALESCE(execution.tenant, ''), gate.task_id, "
        "gate.current_sequence, gate.updated_at "
        "FROM agent_platform.sequence_gate AS gate "
        "JOIN agent_platform.executions AS execution ON execution.task_id = gate.task_id "
        "WHERE gate.tenant IS NULL"
    )
    op.execute(
        "DELETE FROM agent_platform.sequence_gate AS gate "
        "WHERE gate.tenant IS NULL AND EXISTS "
        "(SELECT 1 FROM agent_platform.executions AS execution "
        "WHERE execution.task_id = gate.task_id)"
    )
    op.execute("UPDATE agent_platform.sequence_gate SET tenant = '' WHERE tenant IS NULL")
    _require_tenant("sequence_gate")
    op.create_primary_key(
        op.f(_GATE_PRIMARY_KEY), "sequence_gate", ["tenant", "task_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint(op.f(_GATE_PRIMARY_KEY), "sequence_gate", type_="primary", schema=SCHEMA)
    op.execute(
        "DELETE FROM agent_platform.sequence_gate AS gate WHERE EXISTS "
        "(SELECT 1 FROM agent_platform.sequence_gate AS higher "
        "WHERE higher.task_id = gate.task_id "
        "AND (higher.current_sequence, higher.tenant) > (gate.current_sequence, gate.tenant))"
    )
    op.drop_column("sequence_gate", "tenant", schema=SCHEMA)
    op.create_primary_key(op.f(_GATE_PRIMARY_KEY), "sequence_gate", ["task_id"], schema=SCHEMA)

    op.drop_constraint(op.f(_TENANT_CLAIMS_UNIQUE), "work_claims", type_="unique", schema=SCHEMA)
    op.execute(
        "DELETE FROM agent_platform.work_claims AS claim WHERE EXISTS "
        "(SELECT 1 FROM agent_platform.work_claims AS kept "
        "WHERE kept.task_id = claim.task_id AND kept.work_unit = claim.work_unit "
        "AND (kept.state = 'claimed', kept.created_at, kept.claim_id) "
        "> (claim.state = 'claimed', claim.created_at, claim.claim_id))"
    )
    op.execute(
        "UPDATE agent_platform.work_claims "
        "SET claim_id = encode(sha256(convert_to(task_id || chr(31) || work_unit, 'UTF8')), 'hex')"
    )
    op.drop_column("work_claims", "tenant", schema=SCHEMA)
    op.create_unique_constraint(
        op.f(_CLAIMS_UNIQUE), "work_claims", ["task_id", "work_unit"], schema=SCHEMA
    )
