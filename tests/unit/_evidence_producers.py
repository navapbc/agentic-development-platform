"""Drivers that run each evidence producer against a recording sink.

The coverage tests and the reader tests both need records that production code built,
so the fakes, the requests, and the drivers live here rather than in either module.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from services.review.evidence import EvidenceRecord
from tests.support import capture_evidence

APPROVER = "reviewer@example.invalid"
SUMMARY = "Implementation summary\n\nAdds the filter."


def fake_pr() -> SimpleNamespace:
    return SimpleNamespace(number=7, url="https://host/pr/7", draft=False, state="open")


def patch_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mark_pr_ready(*args, **kwargs):
        return fake_pr()

    monkeypatch.setattr(
        pr_act, "_build_mediator", lambda: SimpleNamespace(mark_pr_ready=mark_pr_ready)
    )


def patch_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    async def record_promotion_approval(*args, **kwargs):
        return {"approved": True, "approver": APPROVER}

    monkeypatch.setattr(
        pr_act,
        "_build_mediator",
        lambda: SimpleNamespace(record_promotion_approval=record_promotion_approval),
    )


class FakeModelResponse:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": "ok"}}],
            "model": "bedrock/claude-x",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }


class FakeModelClient:
    async def __aenter__(self) -> FakeModelClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def post(self, url: str, json: dict, headers: dict) -> FakeModelResponse:
        return FakeModelResponse()


def patch_model_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bedrock.httpx, "AsyncClient", lambda **_: FakeModelClient())


def patch_checks(monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0) -> None:
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


def plan_request() -> PlanEvidenceRequest:
    return PlanEvidenceRequest(
        execution_id="exec-1",
        task_id="T-1",
        plan="1. Read the module.\n2. Add the test.\n",
        verdict="pass",
    )


def promotion_request(summary: str = SUMMARY) -> PromotePrRequest:
    return PromotePrRequest(
        repo="agentic-demo-service",
        task_id="T-1",
        execution_id="exec-2",
        number=7,
        sequence=3,
        summary=summary,
    )


def approval_request() -> RecordApprovalRequest:
    return RecordApprovalRequest(
        repo="agentic-demo-service",
        task_id="T-1",
        execution_id="exec-3",
        number=7,
        approved=True,
        approver="requested@example.invalid",
        sequence=4,
    )


def model_request() -> bedrock.ModelRequest:
    return bedrock.ModelRequest(
        logical_model="planning-model",
        prompt="Plan the change.",
        execution_id="exec-4",
        task_id="T-1",
        stage="plan",
        prompt_version="v2",
    )


def checks_request(workspace: Path) -> RunChecksRequest:
    return RunChecksRequest(
        repo="agentic-demo-service",
        execution_id="exec-5",
        workspace_path=str(workspace),
        commit_sha="a" * 40,
        changed_paths=("a.py",),
    )


async def produce_plan_record(monkeypatch: pytest.MonkeyPatch) -> EvidenceRecord:
    records = capture_evidence(monkeypatch)
    await record_plan_evidence(plan_request())
    return records[-1]


async def produce_summary_record(monkeypatch: pytest.MonkeyPatch) -> EvidenceRecord:
    patch_promotion(monkeypatch)
    records = capture_evidence(monkeypatch)
    await promote_pr(promotion_request())
    return records[-1]


async def produce_approval_record(monkeypatch: pytest.MonkeyPatch) -> EvidenceRecord:
    patch_approval(monkeypatch)
    records = capture_evidence(monkeypatch)
    await record_pr_approval(approval_request())
    return records[-1]


async def produce_prompt_record(monkeypatch: pytest.MonkeyPatch) -> EvidenceRecord:
    patch_model_client(monkeypatch)
    records = capture_evidence(monkeypatch)
    await bedrock.invoke_model(model_request())
    return records[-1]


async def produce_checks_record(monkeypatch: pytest.MonkeyPatch) -> EvidenceRecord:
    patch_checks(monkeypatch)
    records = capture_evidence(monkeypatch)
    with tempfile.TemporaryDirectory() as workspace:
        (Path(workspace) / "a.py").write_text("x = 1\n")
        await run_checks(checks_request(Path(workspace)))
    return records[-1]


PRODUCERS = {
    "plan": produce_plan_record,
    "pull-request-summary": produce_summary_record,
    "approval": produce_approval_record,
    "prompt-reference": produce_prompt_record,
    "build-and-test": produce_checks_record,
}
