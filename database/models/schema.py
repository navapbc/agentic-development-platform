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
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
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
            "CHECKS_RUNNING",
            "REPAIRING",
            "AWAITING_HUMAN",
            "REVIEW",
            "CODE_REVIEW",
            "SECURITY_REVIEW",
            "ACCEPTANCE_VERIFICATION",
            "POLICY_EVALUATION",
            "AWAITING_PR_APPROVAL",
            "PR_CREATING",
            "PR_CREATED",
            "AWAITING_PR_CLOSE",
            "AWAITING_DEPENDENCY",
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
    Column("tenant", String(32), nullable=True, index=True),
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
    Column("event_id", String(255), primary_key=True),
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
    Column("remedy", Text, nullable=True),
    Column("alert_channels", JSON, nullable=True),
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
    Column("content_hash", String(128), nullable=False),
    Column("source_component", String(64), nullable=False),
    Column("classification", String(32), nullable=True),
    Column("retention", String(32), nullable=True),
    Column("location", Text, nullable=True),  # filesystem path or object ref
    Column("metadata", JSON, nullable=True),
    _created_at(),
    Column("tenant", String(32), nullable=False, server_default="", index=True),
    UniqueConstraint(
        "execution_id", "content_hash", name="uq_evidence_artifacts_execution_content_hash"
    ),
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
    Column("owner_workflow_id", String(255), nullable=True),
    Column("state", String(32), nullable=False, server_default="claimed"),
    Column("claim_version", Integer, nullable=False, server_default="1"),
    _created_at(),
    Column("released_at", DateTime(timezone=True), nullable=True),
    Column("tenant", String(32), nullable=False, server_default=""),
    UniqueConstraint("tenant", "task_id", "work_unit", name="uq_work_claims_tenant_task_work_unit"),
)


sequence_gate = Table(
    "sequence_gate",
    metadata,
    Column("task_id", String(64), nullable=False),
    Column("current_sequence", BigInteger, nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    Column("tenant", String(32), nullable=False, server_default=""),
    PrimaryKeyConstraint("tenant", "task_id"),
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
    Column("tenant", String(32), nullable=False, server_default=""),
    _created_at(),
)


side_effects = Table(
    "side_effects",
    metadata,
    Column("side_effect_key", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False, index=True),
    Column("execution_id", String(64), nullable=False, index=True),
    Column("operation", String(64), nullable=False),
    Column("target", String(512), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),  # pending | completed
    Column("result", JSON, nullable=True),
    _created_at(),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


suspended_flows = Table(
    "suspended_flows",
    metadata,
    Column("workflow_id", String(255), primary_key=True),
    Column("reason", String(32), nullable=False),
    Column("resume_on", String(32), nullable=False),
    Column("parked_build_id", String(128), nullable=False, server_default=""),
    Column("stage", String(64), nullable=False, server_default=""),
    Column("detail", Text, nullable=True),
    Column("suspended_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


usage_metrics = Table(
    "usage_metrics",
    metadata,
    Column("execution_id", String(64), primary_key=True),
    Column("stage", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False, index=True),
    Column("workflow_id", String(255), nullable=True),
    Column("model_id", String(128), nullable=True),
    Column("prompt_tokens", BigInteger, nullable=False, server_default="0"),
    Column("completion_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_read_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_write_tokens", BigInteger, nullable=False, server_default="0"),
    Column("total_tokens", BigInteger, nullable=False, server_default="0"),
    Column("llm_calls", Integer, nullable=False, server_default="0"),
    Column("tool_calls", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("elapsed_seconds", Numeric(12, 3), nullable=False, server_default="0"),
    _created_at(),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


run_file_impact = Table(
    "run_file_impact",
    metadata,
    Column("task_id", String(64), primary_key=True),
    Column("execution_id", String(64), nullable=False),
    Column("paths", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
)


slack_approver_groups = Table(
    "slack_approver_groups",
    metadata,
    Column("tenant", String(32), nullable=False),
    Column("group_id", String(32), nullable=False),
    Column("members", JSON, nullable=False),
    Column("refreshed_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("tenant", "group_id"),
)


model_path_health = Table(
    "model_path_health",
    metadata,
    Column("tenant", String(32), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("hop", String(32), nullable=False, server_default=""),
    Column("episode", String(64), nullable=False),
    Column("since", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    Column("slack_channel", String(32), nullable=False, server_default=""),
    Column("slack_ts", String(32), nullable=False, server_default=""),
    Column("admin_slack_channel", String(32), nullable=False, server_default=""),
    Column("admin_slack_ts", String(32), nullable=False, server_default=""),
    Column("admin_slack_token", String(64), nullable=False, server_default=""),
    Column("slack_blocks", Text, nullable=False, server_default=""),
    Column("admin_slack_blocks", Text, nullable=False, server_default=""),
    Column("acknowledged_by", String(128), nullable=False, server_default=""),
    Column("acknowledged_at", DateTime(timezone=True)),
    Column("resolved_by", String(128), nullable=False, server_default=""),
    Column("resolved_at", DateTime(timezone=True)),
    Column("host_action", String(500), nullable=False, server_default=""),
    Column("host_action_at", DateTime(timezone=True)),
    Column("host_action_posted_at", DateTime(timezone=True)),
)


limit_grants = Table(
    "limit_grants",
    metadata,
    Column("request_id", String(64), primary_key=True),
    Column("tenant", String(32), nullable=False),
    Column("limit_name", String(64), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("execution_id", String(64), nullable=False, server_default=""),
    Column("period_start", DateTime(timezone=True)),
    Column("amount", Numeric(20, 4), nullable=False),
    Column("state", String(16), nullable=False),
    Column("requester", String(128), nullable=False),
    Column("approver", String(128), nullable=False, server_default=""),
    _created_at(),
    Column("decided_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("admin_slack_ts", String(32), nullable=False, server_default=""),
    Column("admin_slack_token", String(64), nullable=False, server_default=""),
    Column("admin_slack_blocks", Text, nullable=False, server_default=""),
    Column("tenant_slack_channel", String(32), nullable=False, server_default=""),
    Column("tenant_slack_ts", String(32), nullable=False, server_default=""),
    Column("tenant_slack_blocks", Text, nullable=False, server_default=""),
    Index("ix_limit_grants_tenant_limit_name", "tenant", "limit_name"),
)


tenant_pauses = Table(
    "tenant_pauses",
    metadata,
    Column("pause_id", String(64), primary_key=True),
    Column("tenant", String(32), nullable=False),
    Column("paused_by", String(128), nullable=False),
    Column("paused_at", DateTime(timezone=True), nullable=False),
    Column("cleared_by", String(128), nullable=False, server_default=""),
    Column("cleared_at", DateTime(timezone=True)),
    Index(
        "uq_tenant_pauses_active_tenant",
        "tenant",
        unique=True,
        postgresql_where=text("cleared_at IS NULL"),
    ),
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
    "side_effects",
    "suspended_flows",
    "usage_metrics",
    "run_file_impact",
    "slack_approver_groups",
    "model_path_health",
    "limit_grants",
    "tenant_pauses",
]
