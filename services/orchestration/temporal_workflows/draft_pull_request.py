"""Temporal orchestrator that drives a bounded task from claim to draft PR and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.pull_request import (
        CommitPushRequest,
        CommitPushResult,
        LinkbackRequest,
        LinkbackResult,
        commit_and_push,
        link_pr_to_issue,
    )
    from services.orchestration.temporal_workflows.branch_sync import (
        BranchSyncInput,
        BranchSyncOutput,
        BranchSyncWorkflow,
    )
    from services.orchestration.temporal_workflows.implementation import (
        EXISTING_PR_REUSED,
        ImplementationInput,
        ImplementationOutput,
        ImplementationWorkflow,
    )
    from services.orchestration.temporal_workflows.pull_request import (
        PullRequestInput,
        PullRequestOutput,
        PullRequestWorkflow,
    )

CLAIM_DENIED = "claim_denied"
IMPL_FAILED = "impl_failed"
SYNC_PAUSED = "sync_paused"
DRAFT_PR_OPEN = "draft_pr_open"

_COMMIT_SEQUENCE = 2
_OPEN_PR_SEQUENCE = 3


@dataclass
class DraftPullRequestInput:
    task_id: str
    execution_id: str
    repo: str
    base: str
    base_sha: str
    task: str
    issue_key: str = ""
    focus: str = ""
    work_unit: str = "unit-1"
    agent_id: str = "agent-a"
    file_impact: list[str] = field(default_factory=list)
    commit_message: str = "Apply bounded agent change"
    title: str = ""
    body: str = ""
    stored_issue: dict[str, Any] = field(default_factory=dict)
    max_cost_usd: float = 5.0
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000
    activity_timeout_seconds: float = 2400.0


@dataclass
class DraftPullRequestOutput:
    execution_id: str
    status: str
    terminal_reason: str = ""
    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    linkback: str = "none"
    reused: bool = False


@workflow.defn
class DraftPullRequestWorkflow:
    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_timeout = timedelta(minutes=5)
        workflow_id = workflow.info().workflow_id

        title = params.title or f"[{params.task_id}] {params.task}"

        async def linkback_existing(pr_url: str, branch: str) -> str:
            if not params.issue_key:
                return "none"
            result: LinkbackResult = await workflow.execute_activity(
                link_pr_to_issue,
                LinkbackRequest(
                    issue_key=params.issue_key,
                    execution_id=params.execution_id,
                    pr_url=pr_url,
                    pr_title=title,
                    repo=params.repo,
                    branch=branch,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            return result.action

        impl: ImplementationOutput = await workflow.execute_child_workflow(
            ImplementationWorkflow.run,
            ImplementationInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                commit=params.base_sha,
                task=params.task,
                base=params.base,
                focus=params.focus,
                work_unit=params.work_unit,
                agent_id=params.agent_id,
                file_impact=list(params.file_impact),
                max_cost_usd=params.max_cost_usd,
                max_llm_calls=params.max_llm_calls,
                max_tokens=params.max_tokens,
                activity_timeout_seconds=params.activity_timeout_seconds,
            ),
            id=f"{workflow_id}-impl",
        )

        if impl.status == EXISTING_PR_REUSED:
            action = await linkback_existing(impl.pr_url, impl.branch)
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=EXISTING_PR_REUSED,
                terminal_reason=EXISTING_PR_REUSED,
                branch=impl.branch,
                pr_number=impl.pr_number,
                pr_url=impl.pr_url,
                linkback=action,
                reused=True,
            )

        if impl.status == "claim_denied":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=CLAIM_DENIED,
                terminal_reason=impl.terminal_reason,
            )

        if impl.status != "implemented":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=IMPL_FAILED,
                terminal_reason=impl.terminal_reason,
                branch=impl.branch,
            )

        sync: BranchSyncOutput = await workflow.execute_child_workflow(
            BranchSyncWorkflow.run,
            BranchSyncInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                base=params.base,
                base_sha=params.base_sha,
                branch=impl.branch,
                workspace_path=impl.workspace_path,
                issue_key=params.issue_key,
                stored_issue=dict(params.stored_issue),
            ),
            id=f"{workflow_id}-sync",
        )
        if sync.status != "synced":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=SYNC_PAUSED,
                terminal_reason=sync.terminal_reason,
                branch=impl.branch,
            )

        commit: CommitPushResult = await workflow.execute_activity(
            commit_and_push,
            CommitPushRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                base=params.base,
                commit=params.base_sha,
                message=params.commit_message,
                sequence=_COMMIT_SEQUENCE,
            ),
            start_to_close_timeout=commit_timeout,
            retry_policy=retry,
        )

        body = params.body or (
            f"Automated draft for {params.issue_key} (execution {params.execution_id})."
        )
        pr: PullRequestOutput = await workflow.execute_child_workflow(
            PullRequestWorkflow.run,
            PullRequestInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                base=params.base,
                branch=commit.branch,
                title=title,
                body=body,
                issue_key=params.issue_key,
                base_sha=params.base_sha,
                sequence=_OPEN_PR_SEQUENCE,
            ),
            id=f"{workflow_id}-pr",
        )

        return DraftPullRequestOutput(
            execution_id=params.execution_id,
            status=DRAFT_PR_OPEN,
            terminal_reason=impl.terminal_reason,
            branch=commit.branch,
            pr_number=pr.number,
            pr_url=pr.url,
            linkback=pr.linkback,
        )
