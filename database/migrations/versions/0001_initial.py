"""initial agent_platform schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agent_platform"

EXECUTION_STATES = (
    "TASK_RECEIVED",
    "TASK_VALIDATED",
    "PLANNING",
    "PLAN_REVIEW",
    "AWAITING_PLAN_APPROVAL",
    "IMPLEMENTING",
    "CI_RUNNING",
    "REPAIRING",
    "CODE_REVIEW",
    "SECURITY_REVIEW",
    "ACCEPTANCE_VERIFICATION",
    "POLICY_EVALUATION",
    "AWAITING_PR_APPROVAL",
    "PR_CREATING",
    "PR_CREATED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # Single enum object so the executions table below emits CREATE TYPE once.
    execution_state = sa.Enum(*EXECUTION_STATES, name="execution_state", schema=SCHEMA)

    op.create_table(
        "executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column(
            "state",
            execution_state,
            nullable=False,
            server_default="TASK_RECEIVED",
        ),
        sa.Column("repository", sa.String(255), nullable=True),
        sa.Column("repository_sha", sa.String(64), nullable=True),
        sa.Column("policy_bundle_version", sa.String(64), nullable=True),
        sa.Column("resolved_models", sa.JSON(), nullable=True),
        sa.Column("prompt_versions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_executions_task_id", "executions", ["task_id"], schema=SCHEMA)

    op.create_table(
        "task_snapshots",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("jira_project", sa.String(64), nullable=False),
        sa.Column("jira_source", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            [f"{SCHEMA}.executions.execution_id"],
            name="fk_task_snapshots_execution_id_executions",
        ),
        sa.UniqueConstraint("execution_id", "version", name="uq_task_snapshots_execution_version"),
        schema=SCHEMA,
    )
    op.create_index("ix_task_snapshots_task_id", "task_snapshots", ["task_id"], schema=SCHEMA)

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(64), nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("prev_state", sa.String(64), nullable=True),
        sa.Column("new_state", sa.String(64), nullable=True),
        sa.Column("decision", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("correlation_ids", sa.JSON(), nullable=True),
        sa.Column("artifact_hashes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_audit_events_task_id", "audit_events", ["task_id"], schema=SCHEMA)
    op.create_index("ix_audit_events_execution_id", "audit_events", ["execution_id"], schema=SCHEMA)

    op.create_table(
        "evidence_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("source_component", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=True),
        sa.Column("retention", sa.String(32), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_evidence_artifacts_content_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_evidence_artifacts_execution_id", "evidence_artifacts", ["execution_id"], schema=SCHEMA)

    op.create_table(
        "work_claims",
        sa.Column("claim_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("work_unit", sa.String(255), nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("file_impact", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="claimed"),
        sa.Column("claim_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "work_unit", name="uq_work_claims_task_work_unit"),
        schema=SCHEMA,
    )

    op.create_table(
        "sequence_gate",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("current_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=SCHEMA,
    )

    op.create_table(
        "orphan_artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("repository", sa.String(255), nullable=True),
        sa.Column("owning_workflow", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="active"),
        sa.Column("ttl_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform_tagged", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in (
        "orphan_artifacts",
        "sequence_gate",
        "work_claims",
        "evidence_artifacts",
        "audit_events",
        "task_snapshots",
        "executions",
    ):
        op.drop_table(table, schema=SCHEMA)
    sa.Enum(name="execution_state", schema=SCHEMA).drop(op.get_bind(), checkfirst=True)
