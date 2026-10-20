from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from services.orchestration.temporal_activities.repair import ApplyCIRepairResult
from services.orchestration.temporal_workflows.draft_pull_request import (
    CI_FAILED,
    CI_PASSED,
    DraftPullRequestInput,
    DraftPullRequestOutput,
    DraftPullRequestWorkflow,
)
from services.orchestration.temporal_workflows.implementation import ImplementationOutput
from services.orchestration.usage_rollup import LLM_CALLS_EXCEEDED


class _BudgetLoop(DraftPullRequestWorkflow):
    def __init__(self, ci_statuses: list[str], repair_applied: bool) -> None:
        super().__init__()
        self.ci_statuses = list(ci_statuses)
        self.ci_runs = 0
        self.repair_applied = repair_applied
        self.audited: list[str] = []

    async def _run_ci(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        attempt: int = 0,
    ) -> DraftPullRequestOutput:
        status = self.ci_statuses[min(self.ci_runs, len(self.ci_statuses) - 1)]
        self.ci_runs += 1
        return replace(outcome, status=status)

    async def _repair(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        attempt: int,
    ) -> ApplyCIRepairResult:
        self._repair_llm_calls = params.max_llm_calls + 1
        return ApplyCIRepairResult(applied=self.repair_applied)

    async def _audit(self, event: Any) -> None:
        self.audited.append(event.event_type)


def _params() -> DraftPullRequestInput:
    return DraftPullRequestInput(
        task_id="ADEV-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        base="main",
        base_sha="a" * 40,
        task="Add a widget",
        max_llm_calls=3,
        max_repair_attempts=5,
    )


def _impl() -> ImplementationOutput:
    return ImplementationOutput(
        execution_id="exec-1", status="implemented", terminal_reason="", claimed=True
    )


async def _run(ci_statuses: list[str], repair_applied: bool = True) -> tuple[Any, Any]:
    loop = _BudgetLoop(ci_statuses, repair_applied)
    outcome = await loop._ci_and_repair(
        _params(), _impl(), DraftPullRequestOutput(execution_id="exec-1", status=CI_FAILED), "wf-1"
    )
    return loop, outcome


@pytest.mark.asyncio
async def test_applied_repair_is_verified_before_the_budget_terminates_the_run() -> None:
    loop, _ = await _run([CI_FAILED, CI_PASSED])

    assert loop.ci_runs == 2


@pytest.mark.asyncio
async def test_budget_breach_after_a_successful_repair_reports_the_repaired_status() -> None:
    loop, outcome = await _run([CI_FAILED, CI_PASSED])

    assert outcome.status == CI_PASSED
    assert outcome.terminal_reason == ""
    assert "REPAIR_BUDGET_EXCEEDED" in loop.audited


@pytest.mark.asyncio
async def test_budget_breach_after_a_failed_repair_still_reports_the_budget_reason() -> None:
    loop, outcome = await _run([CI_FAILED, CI_FAILED])

    assert outcome.status == CI_FAILED
    assert outcome.terminal_reason == LLM_CALLS_EXCEEDED
    assert loop.ci_runs == 2


@pytest.mark.asyncio
async def test_budget_breach_without_an_applied_repair_runs_no_further_checks() -> None:
    loop, outcome = await _run([CI_FAILED, CI_PASSED], repair_applied=False)

    assert loop.ci_runs == 1
    assert outcome.status == CI_FAILED
    assert outcome.terminal_reason == LLM_CALLS_EXCEEDED
