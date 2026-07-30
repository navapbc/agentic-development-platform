"""Unit tests for the platform's own database schema."""

from __future__ import annotations

from database.models import SCHEMA, metadata

EXPECTED_TABLES = {
    "executions",
    "task_snapshots",
    "audit_events",
    "evidence_artifacts",
    "work_claims",
    "sequence_gate",
    "orphan_artifacts",
    "side_effects",
}


def test_schema_name_is_agent_platform():
    assert SCHEMA == "agent_platform"
    assert metadata.schema == "agent_platform"


def test_all_phase1_tables_present():
    assert {t.name for t in metadata.sorted_tables} == EXPECTED_TABLES


def test_every_table_bound_to_agent_platform_schema():
    for table in metadata.tables.values():
        assert table.schema == "agent_platform", table.name


def test_task_snapshot_is_immutable_per_execution_version():
    snapshots = metadata.tables["agent_platform.task_snapshots"]
    unique_cols = {
        tuple(c.name for c in constraint.columns)
        for constraint in snapshots.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("execution_id", "version") in unique_cols


def test_work_claim_unique_per_task_work_unit():
    claims = metadata.tables["agent_platform.work_claims"]
    unique_cols = {
        tuple(c.name for c in constraint.columns)
        for constraint in claims.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("task_id", "work_unit") in unique_cols


def test_evidence_is_content_addressed():
    evidence = metadata.tables["agent_platform.evidence_artifacts"]
    assert evidence.c.content_hash.unique is True
