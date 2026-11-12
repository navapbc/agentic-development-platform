"""Conformance tests for the committed audit-event and evidence-object schemas.

Each schema in ``schemas/`` is a versioned contract for a durable record shape. These
tests validate a record built from the production dataclasses that write the record
against its committed schema, so a drift between the write shape and the contract fails
here.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from services.orchestration.temporal_activities.persistence import AuditEvent
from services.review.evidence import EvidenceRecord

_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SCHEMA = _ROOT / "schemas/audit_events/audit-event.schema.json"
_EVIDENCE_SCHEMA = _ROOT / "schemas/evidence/evidence-object.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _audit_record() -> dict:
    event = AuditEvent(
        event_id="exec-1:REVIEW_COMPLETED:code-review",
        task_id="T-1",
        execution_id="exec-1",
        event_type="REVIEW_COMPLETED",
        stage="REVIEW",
        prev_state="REVIEW",
        new_state="REVIEW",
        model_id="us.anthropic.claude-opus-4-5-20251101-v1:0",
        reason="code review passed",
        workflow_id="draft-pr-T-1",
        decision="pass",
        prompt_version="v2",
        artifact_hashes={"attestation": "sha256:abc"},
    )
    return dataclasses.asdict(event)


def _evidence_record() -> dict:
    record = EvidenceRecord(
        artifact_id="art-1",
        execution_id="exec-1",
        content_hash="sha256:deadbeef",
        source_component="gate:code-review",
        classification="standard",
        retention="2555",
        location="s3://evidence/sha256/deadbeef",
        metadata={"stage": "code-review"},
    )
    return dataclasses.asdict(record)


def test_audit_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(_load(_AUDIT_SCHEMA))


def test_evidence_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(_load(_EVIDENCE_SCHEMA))


def test_schemas_carry_a_version():
    assert _load(_AUDIT_SCHEMA)["version"]
    assert _load(_EVIDENCE_SCHEMA)["version"]


def test_audit_event_write_shape_conforms():
    Draft202012Validator(_load(_AUDIT_SCHEMA)).validate(_audit_record())


def test_evidence_record_write_shape_conforms():
    Draft202012Validator(_load(_EVIDENCE_SCHEMA)).validate(_evidence_record())


def test_audit_event_fields_are_all_in_the_schema():
    props = set(_load(_AUDIT_SCHEMA)["properties"])
    assert {f.name for f in dataclasses.fields(AuditEvent)} <= props


def test_evidence_record_fields_are_all_in_the_schema():
    props = set(_load(_EVIDENCE_SCHEMA)["properties"])
    assert {f.name for f in dataclasses.fields(EvidenceRecord)} <= props


def test_audit_event_missing_required_field_fails():
    record = _audit_record()
    del record["event_type"]
    with pytest.raises(ValidationError):
        Draft202012Validator(_load(_AUDIT_SCHEMA)).validate(record)


def test_evidence_record_missing_required_field_fails():
    record = _evidence_record()
    del record["content_hash"]
    with pytest.raises(ValidationError):
        Draft202012Validator(_load(_EVIDENCE_SCHEMA)).validate(record)
