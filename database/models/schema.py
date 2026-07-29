"""SQLAlchemy Core schema for the platform's agent_platform database."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

SCHEMA = "agent_platform"

# Deterministic constraint and index names so autogenerate produces stable diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


def _created_at() -> Column:
    return Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)


executions = Table(
    "executions",
    metadata,
    Column("execution_id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False, index=True),
    Column("workflow_id", String(255), nullable=True),
    Column(
        "state",
        Enum(
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
            name="execution_state",
            schema=SCHEMA,
        ),
        nullable=False,
        server_default="TASK_RECEIVED",
    ),
    Column("repository", String(255), nullable=True),
    Column("repository_sha", String(64), nullable=True),
    Column("policy_bundle_version", String(64), nullable=True),
    Column("resolved_models", JSON, nullable=True),
    Column("prompt_versions", JSON, nullable=True),
    _created_at(),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


task_snapshots = Table(
    "task_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("execution_id", String(64), ForeignKey(executions.c.execution_id), nullable=False),
    Column("task_id", String(64), nullable=False, index=True),
    Column("jira_project", String(64), nullable=False),
    Column("jira_source", String(32), nullable=False),  # cloud | datacenter
    Column("version", Integer, nullable=False, server_default="1"),
    Column("content_hash", String(128), nullable=False),
    Column("payload", JSON, nullable=False),
    _created_at(),
    UniqueConstraint("execution_id", "version", name="execution_version"),
)


audit_events = Table(
    "audit_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False, index=True),
    Column("execution_id", String(64), nullable=False, index=True),
    Column("workflow_id", String(255), nullable=True),
    Column("event_type", String(64), nullable=False),
    Column("stage", String(64), nullable=True),
    Column("actor_type", String(32), nullable=True),
    Column("actor_id", String(128), nullable=True),
    Column("prev_state", String(64), nullable=True),
    Column("new_state", String(64), nullable=True),
    Column("decision", String(64), nullable=True),
    Column("reason", Text, nullable=True),
    Column("prompt_version", String(64), nullable=True),
    Column("model_id", String(128), nullable=True),
    Column("policy_version", String(64), nullable=True),
    Column("correlation_ids", JSON, nullable=True),
    Column("artifact_hashes", JSON, nullable=True),
    _created_at(),
)


evidence_artifacts = Table(
    "evidence_artifacts",
    metadata,
    Column("artifact_id", String(64), primary_key=True),
    Column("execution_id", String(64), nullable=False, index=True),
    Column("content_hash", String(128), nullable=False, unique=True),
    Column("source_component", String(64), nullable=False),
    Column("classification", String(32), nullable=True),
    Column("retention", String(32), nullable=True),
    Column("location", Text, nullable=True),  # filesystem path or object ref
    Column("metadata", JSON, nullable=True),
    _created_at(),
)


work_claims = Table(
    "work_claims",
    metadata,
    Column("claim_id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False),
    Column("work_unit", String(255), nullable=False),
    Column("execution_id", String(64), nullable=False),
    Column("agent_id", String(128), nullable=False),
    Column("file_impact", JSON, nullable=True),
    Column("state", String(32), nullable=False, server_default="claimed"),
    Column("claim_version", Integer, nullable=False, server_default="1"),
    _created_at(),
    Column("released_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("task_id", "work_unit", name="task_work_unit"),
)


sequence_gate = Table(
    "sequence_gate",
    metadata,
    Column("task_id", String(64), primary_key=True),
    Column("current_sequence", BigInteger, nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


orphan_artifacts = Table(
    "orphan_artifacts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("artifact_type", String(32), nullable=False),  # branch | pr | build | evidence | staging
    Column("external_id", String(512), nullable=False),
    Column("repository", String(255), nullable=True),
    Column("owning_workflow", String(255), nullable=True),
    Column("state", String(32), nullable=False, server_default="active"),
    Column("ttl_at", DateTime(timezone=True), nullable=True),
    Column("platform_tagged", Boolean, nullable=False, server_default="true"),
    _created_at(),
)


__all__ = [
    "SCHEMA",
    "metadata",
    "executions",
    "task_snapshots",
    "audit_events",
    "evidence_artifacts",
    "work_claims",
    "sequence_gate",
    "orphan_artifacts",
]
