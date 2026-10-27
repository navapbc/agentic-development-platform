"""Evidence coverage for plans, pull-request summaries, approvals, prompts, and checks.

Each producer is driven directly with the sink replaced by a recorder, so the assertions
cover the source component, the execution id, and the idempotent content address.
"""

from __future__ import annotations

import pytest
from services.orchestration.temporal_activities import bedrock
from services.orchestration.temporal_activities.checks import RunChecksRequest, run_checks
from services.orchestration.temporal_activities.planning import record_plan_evidence
from services.orchestration.temporal_activities.pull_request import (
    promote_pr,
    record_pr_approval,
)
from services.review.classification import CLASSIFICATION_AUDIT, CLASSIFICATION_STANDARD
from services.review.evidence import (
    APPROVAL_COMPONENT,
    BUILD_AND_TEST_COMPONENT,
    PLAN_COMPONENT,
    PROMPT_REFERENCE_COMPONENT,
    PULL_REQUEST_SUMMARY_COMPONENT,
)
from tests.support import capture_evidence
from tests.unit._evidence_producers import (
    APPROVER,
    PRODUCERS,
    approval_request,
    model_request,
    patch_approval,
    patch_checks,
    patch_model_client,
    patch_promotion,
    plan_request,
    promotion_request,
)


async def test_accepted_plan_writes_a_plan_evidence_record(monkeypatch):
    records = capture_evidence(monkeypatch)

    result = await record_plan_evidence(plan_request())

    assert len(records) == 1
    assert records[0].artifact_id == result.artifact_id
    assert records[0].source_component == PLAN_COMPONENT
    assert records[0].execution_id == "exec-1"
    assert records[0].classification == CLASSIFICATION_STANDARD
    assert records[0].content_hash.startswith("sha256:")
    assert records[0].metadata["task_id"] == "T-1"
    assert records[0].metadata["verdict"] == "pass"


async def test_promotion_writes_the_pull_request_summary(monkeypatch):
    patch_promotion(monkeypatch)
    records = capture_evidence(monkeypatch)

    result = await promote_pr(promotion_request())

    assert result.ready is True
    assert len(records) == 1
    assert records[0].source_component == PULL_REQUEST_SUMMARY_COMPONENT
    assert records[0].execution_id == "exec-2"
    assert records[0].classification == CLASSIFICATION_STANDARD
    assert records[0].metadata["number"] == 7
    assert records[0].metadata["url"] == "https://host/pr/7"


async def test_promotion_without_a_summary_writes_nothing(monkeypatch):
    patch_promotion(monkeypatch)
    records = capture_evidence(monkeypatch)

    await promote_pr(promotion_request(summary=""))

    assert records == []


async def test_approval_writes_an_audit_classified_record(monkeypatch):
    patch_approval(monkeypatch)
    records = capture_evidence(monkeypatch)

    result = await record_pr_approval(approval_request())

    assert result.approved is True
    assert len(records) == 1
    assert records[0].source_component == APPROVAL_COMPONENT
    assert records[0].execution_id == "exec-3"
    assert records[0].classification == CLASSIFICATION_AUDIT
    assert records[0].metadata["approver"] == APPROVER
    assert records[0].metadata["approved"] is True


async def test_a_model_call_writes_a_prompt_reference(monkeypatch):
    patch_model_client(monkeypatch)
    records = capture_evidence(monkeypatch)

    await bedrock.invoke_model(model_request())

    assert len(records) == 1
    assert records[0].source_component == PROMPT_REFERENCE_COMPONENT
    assert records[0].execution_id == "exec-4"
    assert records[0].classification == CLASSIFICATION_STANDARD
    assert records[0].metadata["prompt_version"] == "v2"
    assert records[0].metadata["model_id"] == "bedrock/claude-x"
    assert len(records[0].metadata["prompt_sha256"]) == 64
    assert "Plan the change." not in str(records[0].metadata)


async def test_a_model_call_without_an_execution_writes_nothing(monkeypatch):
    patch_model_client(monkeypatch)
    records = capture_evidence(monkeypatch)

    await bedrock.invoke_model(
        bedrock.ModelRequest(logical_model="planning-model", prompt="Plan the change.")
    )

    assert records == []


async def test_a_check_run_writes_a_build_and_test_report(tmp_path, monkeypatch):
    patch_checks(monkeypatch)
    records = capture_evidence(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n")

    result = await run_checks(
        RunChecksRequest(
            repo="agentic-demo-service",
            execution_id="exec-5",
            workspace_path=str(workspace),
            commit_sha="a" * 40,
            changed_paths=("a.py",),
        )
    )

    assert result.ran is True
    assert len(records) == 1
    assert records[0].source_component == BUILD_AND_TEST_COMPONENT
    assert records[0].execution_id == "exec-5"
    assert records[0].classification == CLASSIFICATION_STANDARD
    assert records[0].metadata["passed"] is True
    assert records[0].metadata["commit_sha"] == "a" * 40
    assert records[0].metadata["changed_path_count"] == 1


async def test_a_failed_check_run_writes_a_report_that_records_the_failure(tmp_path, monkeypatch):
    patch_checks(monkeypatch, exit_code=1)
    records = capture_evidence(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n")

    await run_checks(
        RunChecksRequest(
            repo="agentic-demo-service",
            execution_id="exec-5",
            workspace_path=str(workspace),
            commit_sha="a" * 40,
            changed_paths=("a.py",),
        )
    )

    assert len(records) == 1
    assert records[0].source_component == BUILD_AND_TEST_COMPONENT
    assert records[0].metadata["passed"] is False
    assert records[0].metadata["exit_code"] == 1


async def test_a_skipped_check_run_writes_nothing(tmp_path, monkeypatch):
    import services.orchestration.checks.config as checks_config_mod

    monkeypatch.setattr(checks_config_mod, "checks_for_repo", lambda repo, settings: None)
    records = capture_evidence(monkeypatch)

    result = await run_checks(
        RunChecksRequest(
            repo="unconfigured",
            execution_id="exec-6",
            workspace_path=str(tmp_path),
            commit_sha="b" * 40,
        )
    )

    assert result.ran is False
    assert records == []


@pytest.mark.parametrize("kind", sorted(PRODUCERS))
async def test_a_replayed_write_keeps_the_same_content_address(kind: str, monkeypatch):
    first = await PRODUCERS[kind](monkeypatch)
    second = await PRODUCERS[kind](monkeypatch)

    assert first.content_hash == second.content_hash
    assert first.artifact_id != second.artifact_id


async def test_a_changed_body_changes_the_content_address():
    """The sink drops conflicting addresses, so a collapsing address loses evidence silently."""
    from services.review.evidence import evidence_content_hash

    base = {"execution_id": "exec-7", "plan": "build the thing"}

    assert evidence_content_hash(base) != evidence_content_hash(
        {**base, "plan": "build the other thing"}
    )
    assert evidence_content_hash(base) != evidence_content_hash({**base, "execution_id": "exec-8"})
