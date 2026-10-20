"""Evidence coverage for plans, pull-request summaries, approvals, prompts, and checks.

Each producer is driven directly with the sink replaced by a recorder, so the assertions
cover the source component, the execution id, and the idempotent content address.
"""

from __future__ import annotations

from types import SimpleNamespace

from services.orchestration.temporal_activities import bedrock
from services.orchestration.temporal_activities import pull_request as pr_act
from services.orchestration.temporal_activities.checks import RunChecksRequest, run_checks
from services.orchestration.temporal_activities.planning import (
    PlanEvidenceRequest,
    record_plan_evidence,
)
from services.orchestration.temporal_activities.pull_request import (
    PromotePrRequest,
    RecordApprovalRequest,
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


def _plan_request() -> PlanEvidenceRequest:
    return PlanEvidenceRequest(
        execution_id="exec-1",
        task_id="T-1",
        plan="1. Read the module.\n2. Add the test.\n",
        verdict="pass",
    )


async def test_accepted_plan_writes_a_plan_evidence_record(monkeypatch):
    records = capture_evidence(monkeypatch)

    result = await record_plan_evidence(_plan_request())

    assert len(records) == 1
    assert records[0].artifact_id == result.artifact_id
    assert records[0].source_component == PLAN_COMPONENT
    assert records[0].execution_id == "exec-1"
    assert records[0].classification == CLASSIFICATION_STANDARD
    assert records[0].content_hash.startswith("sha256:")
    assert records[0].metadata["task_id"] == "T-1"
    assert records[0].metadata["verdict"] == "pass"


async def test_a_replayed_plan_write_keeps_the_same_content_address(monkeypatch):
    records = capture_evidence(monkeypatch)

    await record_plan_evidence(_plan_request())
    await record_plan_evidence(_plan_request())

    assert records[0].content_hash == records[1].content_hash
    assert records[0].artifact_id != records[1].artifact_id


def _fake_pr() -> SimpleNamespace:
    return SimpleNamespace(number=7, url="https://host/pr/7", draft=False, state="open")


def _promotion(summary: str) -> PromotePrRequest:
    return PromotePrRequest(
        repo="agentic-demo-service",
        task_id="T-1",
        execution_id="exec-2",
        number=7,
        sequence=3,
        summary=summary,
    )


async def test_promotion_writes_the_pull_request_summary(monkeypatch):
    async def mark_pr_ready(*args, **kwargs):
        return _fake_pr()

    monkeypatch.setattr(
        pr_act, "_build_mediator", lambda: SimpleNamespace(mark_pr_ready=mark_pr_ready)
    )
    records = capture_evidence(monkeypatch)

    result = await promote_pr(_promotion("Implementation summary\n\nAdds the filter."))

    assert result.ready is True
    assert len(records) == 1
    assert records[0].source_component == PULL_REQUEST_SUMMARY_COMPONENT
    assert records[0].execution_id == "exec-2"
    assert records[0].metadata["number"] == 7
    assert records[0].metadata["url"] == "https://host/pr/7"


async def test_promotion_without_a_summary_writes_nothing(monkeypatch):
    async def mark_pr_ready(*args, **kwargs):
        return _fake_pr()

    monkeypatch.setattr(
        pr_act, "_build_mediator", lambda: SimpleNamespace(mark_pr_ready=mark_pr_ready)
    )
    records = capture_evidence(monkeypatch)

    await promote_pr(_promotion(""))

    assert records == []


async def test_approval_writes_an_audit_classified_record(monkeypatch):
    async def record_promotion_approval(*args, **kwargs):
        return {"approved": True, "approver": "reviewer@example.invalid"}

    monkeypatch.setattr(
        pr_act,
        "_build_mediator",
        lambda: SimpleNamespace(record_promotion_approval=record_promotion_approval),
    )
    records = capture_evidence(monkeypatch)

    result = await record_pr_approval(
        RecordApprovalRequest(
            repo="agentic-demo-service",
            task_id="T-1",
            execution_id="exec-3",
            number=7,
            approved=True,
            approver="requested@example.invalid",
            sequence=4,
        )
    )

    assert result.approved is True
    assert len(records) == 1
    assert records[0].source_component == APPROVAL_COMPONENT
    assert records[0].execution_id == "exec-3"
    assert records[0].classification == CLASSIFICATION_AUDIT
    assert records[0].metadata["approver"] == "reviewer@example.invalid"
    assert records[0].metadata["approved"] is True


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": "ok"}}],
            "model": "bedrock/claude-x",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        return _FakeResponse()


async def test_a_model_call_writes_a_prompt_reference(monkeypatch):
    monkeypatch.setattr(bedrock.httpx, "AsyncClient", lambda **_: _FakeClient())
    records = capture_evidence(monkeypatch)

    await bedrock.invoke_model(
        bedrock.ModelRequest(
            logical_model="planning-model",
            prompt="Plan the change.",
            execution_id="exec-4",
            task_id="T-1",
            stage="plan",
            prompt_version="v2",
        )
    )

    assert len(records) == 1
    assert records[0].source_component == PROMPT_REFERENCE_COMPONENT
    assert records[0].execution_id == "exec-4"
    assert records[0].metadata["prompt_version"] == "v2"
    assert records[0].metadata["model_id"] == "bedrock/claude-x"
    assert len(records[0].metadata["prompt_sha256"]) == 64
    assert "Plan the change." not in str(records[0].metadata)


async def test_a_model_call_without_an_execution_writes_nothing(monkeypatch):
    monkeypatch.setattr(bedrock.httpx, "AsyncClient", lambda **_: _FakeClient())
    records = capture_evidence(monkeypatch)

    await bedrock.invoke_model(
        bedrock.ModelRequest(logical_model="planning-model", prompt="Plan the change.")
    )

    assert records == []


def _patch_checks(monkeypatch, exit_code: int) -> None:
    import services.orchestration.checks.config as checks_config_mod
    import services.orchestration.checks.runner as runner_mod
    import services.orchestration.gates.pod_runner as pod_mod
    from services.orchestration.checks.config import ChecksConfig

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_checks(self, spec):
            return SimpleNamespace(exit_code=exit_code, output="3 passed")

    monkeypatch.setattr(
        checks_config_mod,
        "checks_for_repo",
        lambda repo, settings: ChecksConfig(
            repo=repo, image="python:3.12-slim", command="pytest -q"
        ),
    )
    monkeypatch.setattr(runner_mod, "ChecksRunner", _Runner)
    monkeypatch.setattr(pod_mod, "KubectlPodOps", lambda *a, **k: object())


async def test_a_check_run_writes_a_build_and_test_report(tmp_path, monkeypatch):
    _patch_checks(monkeypatch, exit_code=0)
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
    assert records[0].metadata["passed"] is True
    assert records[0].metadata["commit_sha"] == "a" * 40
    assert records[0].metadata["changed_path_count"] == 1


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
