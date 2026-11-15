"""Temporal workflow that takes one Jira issue into intake on its tenant's task queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeoutError

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.issue_intake import (
        JiraIssueIntakeReport,
        JiraIssueIntakeRequest,
        intake_jira_issue,
    )

INTAKE_TIMEOUT = timedelta(minutes=10)
INTAKE_NOT_STARTED = "IntakeNotStarted"


@dataclass
class JiraIssueIntakeInput:
    issue_key: str


def _not_started(error: ActivityError) -> bool:
    cause = error.cause
    return isinstance(cause, ActivityTimeoutError) and cause.type == TimeoutType.SCHEDULE_TO_START


@workflow.defn
class JiraIssueIntakeWorkflow:
    @workflow.run
    async def run(self, params: JiraIssueIntakeInput) -> JiraIssueIntakeReport:
        try:
            return await workflow.execute_activity(
                intake_jira_issue,
                JiraIssueIntakeRequest(issue_key=params.issue_key),
                schedule_to_start_timeout=INTAKE_TIMEOUT,
                start_to_close_timeout=INTAKE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            if not _not_started(exc):
                raise
            raise ApplicationError(
                f"no activity worker took the intake of {params.issue_key} from task queue "
                f"{workflow.info().task_queue} within {INTAKE_TIMEOUT.total_seconds():.0f} "
                "seconds. Check that the activity worker of the queue's tenant is running.",
                type=INTAKE_NOT_STARTED,
                non_retryable=True,
            ) from exc
