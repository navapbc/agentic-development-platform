"""Temporal workflow for draft PR creation and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )
    from services.orchestration.temporal_activities.pull_request import (
        LinkbackRequest,
        LinkbackResult,
        OpenPrRequest,
        OpenPrResult,
        create_draft_pr,
        link_pr_to_issue,
    )

PR_CREATING = "PR_CREATING"
PR_CREATED = "PR_CREATED"


@dataclass
class PullRequestInput:
    task_id: str
    execution_id: str
    repo: str
    base: str
    branch: str
    title: str
    body: str = ""
    issue_key: str = ""
    base_sha: str = ""
    sequence: int = 1


@dataclass
class PullRequestOutput:
    execution_id: str
    number: int
    url: str
    created: bool
    linkback: str = "none"
    orphan_id: str = ""


@workflow.defn
class PullRequestWorkflow:
    @workflow.run
    async def run(self, params: PullRequestInput) -> PullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        pr_timeout = timedelta(minutes=2)
        workflow_id = workflow.info().workflow_id

        async def persist(state: str) -> None:
            await workflow.execute_activity(
                persist_execution_state,
                ExecutionState(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    state=state,
                    workflow_id=workflow_id,
                    repository=params.repo or None,
                    repository_sha=params.base_sha or None,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        async def audit(event: AuditEvent) -> None:
            await workflow.execute_activity(
                record_audit_event,
                event,
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        await persist(PR_CREATING)
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:PR_CREATION_AUTHORIZED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="PR_CREATION_AUTHORIZED",
                stage="PR_CREATING",
                new_state=PR_CREATING,
                decision="authorize",
                reason=f"opening draft PR for {params.branch} against {params.base}",
                workflow_id=workflow_id,
            )
        )

        pr: OpenPrResult = await workflow.execute_activity(
            create_draft_pr,
            OpenPrRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                head=params.branch,
                base=params.base,
                title=params.title,
                body=params.body,
                sequence=params.sequence,
                workflow_id=workflow_id,
            ),
            start_to_close_timeout=pr_timeout,
            retry_policy=retry,
        )

        await persist(PR_CREATED)
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:PR_CREATED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="PR_CREATED",
                stage="PR_CREATED",
                prev_state=PR_CREATING,
                new_state=PR_CREATED,
                decision="created" if pr.created else "updated",
                reason=f"draft PR #{pr.number} at {pr.url}",
                workflow_id=workflow_id,
                artifact_hashes={"pr_url": pr.url, "orphan_id": pr.orphan_id},
            )
        )

        linkback = "none"
        if params.issue_key:
            result: LinkbackResult = await workflow.execute_activity(
                link_pr_to_issue,
                LinkbackRequest(
                    issue_key=params.issue_key,
                    execution_id=params.execution_id,
                    pr_url=pr.url,
                    pr_title=params.title,
                    repo=params.repo,
                    branch=params.branch,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            linkback = result.action
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:PR_LINKED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="PR_LINKED",
                    stage="PR_CREATED",
                    new_state=PR_CREATED,
                    decision=result.action,
                    reason=f"jira linkback {result.action} on {params.issue_key}",
                    workflow_id=workflow_id,
                )
            )

        return PullRequestOutput(
            execution_id=params.execution_id,
            number=pr.number,
            url=pr.url,
            created=pr.created,
            linkback=linkback,
            orphan_id=pr.orphan_id,
        )
