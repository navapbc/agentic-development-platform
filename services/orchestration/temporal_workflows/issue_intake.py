"""Temporal workflow that takes one Jira issue into intake on its tenant's task queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.issue_intake import (
        JiraIssueIntakeReport,
        JiraIssueIntakeRequest,
        intake_jira_issue,
    )


@dataclass
class JiraIssueIntakeInput:
    issue_key: str


@workflow.defn
class JiraIssueIntakeWorkflow:
    @workflow.run
    async def run(self, params: JiraIssueIntakeInput) -> JiraIssueIntakeReport:
        return await workflow.execute_activity(
            intake_jira_issue,
            JiraIssueIntakeRequest(issue_key=params.issue_key),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
