"""The workflows a workflow worker registers and the sandbox that runs them.

A replay that decides whether a run can move to this build uses the same workflows and sandbox as
the worker that runs it afterwards.
"""

from __future__ import annotations

from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from services.orchestration.temporal_workflows import (
    BranchSyncWorkflow,
    CountingWorkflow,
    DraftPullRequestWorkflow,
    DurableTaskWorkflow,
    ImplementationWorkflow,
    IntakeWorkflow,
    JiraIssueIntakeWorkflow,
    PlanReviewWorkflow,
    PullRequestWorkflow,
    ReapWorkflow,
    TenantValidationWorkflow,
)
from services.orchestration.temporal_workflows.alert_action import AlertActionWorkflow

WORKFLOWS = [
    DurableTaskWorkflow,
    CountingWorkflow,
    PlanReviewWorkflow,
    ImplementationWorkflow,
    BranchSyncWorkflow,
    PullRequestWorkflow,
    DraftPullRequestWorkflow,
    ReapWorkflow,
    IntakeWorkflow,
    JiraIssueIntakeWorkflow,
    TenantValidationWorkflow,
    AlertActionWorkflow,
]


def workflow_runner() -> SandboxedWorkflowRunner:
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "beartype", "opentelemetry"
        )
    )
