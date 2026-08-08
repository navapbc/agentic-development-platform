"""Phase 6 exit proof: the full draft-PR-to-merged scenario in a single hermetic run.

This composes every Phase 6 stage into one workflow execution on the Temporal test
environment, the primary Jenkins route, and adds a provider-parity check that drives
the same CIHost contract against the fake GitHub Actions fixture. The committed gate is
hermetic. The live no-mocks path is exercised by `inv demo-draft-pr --full-pipeline`
and the env-gated backend smokes.
"""

from __future__ import annotations

import asyncio
import uuid

from adapters.interfaces.ci_host import FAILURE, SUCCESS
from services.orchestration.suspension import SuspendReason
from services.orchestration.temporal_workflows.draft_pull_request import (
    COMPLETED,
    PR_MERGED,
    DraftPullRequestInput,
    DraftPullRequestWorkflow,
)
from temporalio.testing import WorkflowEnvironment
from tests.integration import test_github_actions_backend as actions_fx
from tests.integration import test_jenkins_real_backend as jenkins_fx
from tests.unit.test_draft_pull_request_workflow import (
    _await_approval_park,
    _make_activities,
    _start_ci_worker,
)


def _exit_input() -> DraftPullRequestInput:
    return DraftPullRequestInput(
        task_id="AGENTDEV-1",
        execution_id="exec-exit",
        repo="agentic-demo-service",
        base="main",
        base_sha="a" * 40,
        task="Implement the change",
        issue_key="AGENTDEV-1",
        file_impact=["feature.py"],
        stored_issue={"summary": "Implement the change"},
        ci_job="agentic-demo-service-ci",
        ci_poll_interval_seconds=1.0,
        ci_poll_max_attempts=5,
        run_reviews=True,
        run_policy=True,
        run_approval=True,
        await_pr_close=True,
        jira_project="AGENTDEV",
        plan_approved=True,
    )


async def _await_pr_close_park(handle) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if state.get("reason") == SuspendReason.AWAITING_PR_CLOSE.value:
            return state
        await asyncio.sleep(0.05)
    return state


async def test_draft_pr_to_merged_passes_every_gate_in_one_run() -> None:
    ci_triggers: list = []
    repairs: list = []
    review_requests: list = []
    scan_requests: list = []
    policy_requests: list = []
    approvals: list = []
    promotions: list = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
        review_requests=review_requests,
        scan_requests=scan_requests,
        policy_requests=policy_requests,
        approvals=approvals,
        promotions=promotions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"exit-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _exit_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            await _await_approval_park(handle)
            await handle.signal("submit_pr_approval", args=[True, "release-manager"])

            state = await _await_pr_close_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_PR_CLOSE.value

            await handle.signal("submit_pr_closed", True)
            result = await handle.result()

    assert result.status == PR_MERGED
    assert result.pr_close_status == COMPLETED
    assert result.pr_merged is True
    assert result.promoted is True

    assert result.repair_attempts == 1
    assert len(ci_triggers) == 2
    assert len(repairs) == 1

    assert result.review_status == "passed"
    assert result.code_verdict == "pass"
    assert result.security_verdict == "pass"
    assert result.acceptance_verdict == "pass"
    assert len(review_requests) == 3

    assert result.policy_status == "allowed"
    assert result.scan_blocking_findings == 0
    assert len(scan_requests) == 1
    assert len(policy_requests) == 1

    assert result.approval_status == "approved"
    assert result.approver == "release-manager"
    assert len(approvals) == 1
    assert len(promotions) == 1


async def test_jenkins_and_actions_satisfy_the_same_ci_contract() -> None:
    jenkins = jenkins_fx._backend()
    actions = actions_fx._backend(actions_fx._fake_actions()[0])

    jenkins_trigger = await jenkins.trigger_build(
        jenkins_fx.REPO,
        jenkins_fx.JOB,
        branch=jenkins_fx.BRANCH,
        commit_sha=jenkins_fx.COMMIT_SHA,
        execution_id=jenkins_fx.EXECUTION_ID,
    )
    actions_trigger = await actions.trigger_build(
        actions_fx.REPO,
        actions_fx.JOB,
        branch=actions_fx.BRANCH,
        commit_sha=actions_fx.COMMIT_SHA,
        execution_id=actions_fx.EXECUTION_ID,
    )
    assert jenkins_trigger.triggered is True
    assert actions_trigger.triggered is True
    assert jenkins_trigger.build_number > 0
    assert actions_trigger.build_number > 0

    jenkins_success = await jenkins.get_build(jenkins_fx.REPO, jenkins_fx.JOB, 7)
    actions_success = await actions.get_build(actions_fx.REPO, actions_fx.JOB, 101)
    assert jenkins_success.result == SUCCESS == actions_success.result
    assert jenkins_success.status == "completed" == actions_success.status
    assert jenkins_success.completed is True is actions_success.completed

    jenkins_failure = await jenkins.get_build(jenkins_fx.REPO, jenkins_fx.JOB, 8)
    actions_failure = await actions.get_build(actions_fx.REPO, actions_fx.JOB, 202)
    assert jenkins_failure.result == FAILURE == actions_failure.result

    jenkins_log = await jenkins.get_build_log(jenkins_fx.REPO, jenkins_fx.JOB, 8)
    actions_log = await actions.get_build_log(actions_fx.REPO, actions_fx.JOB, 202)
    assert "AssertionError: boom" in jenkins_log.failure_summary
    assert "AssertionError: boom" in actions_log.failure_summary
