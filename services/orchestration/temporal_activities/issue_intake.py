"""Temporal activity that takes one Jira issue into intake, by key, in its tenant's worker.

The inbound webhook hands over only the issue key. The activity runs in the activity worker of the
tenant whose task queue the workflow runs on, reads the issue from Jira with that tenant's
credentials through the poll's candidate query narrowed to the key, and routes it through the
shared intake path, so an issue the poll would not take is not taken here either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,254}-[1-9][0-9]{0,17}$")


def valid_issue_key(key: str) -> bool:
    return bool(ISSUE_KEY.fullmatch(key))


@dataclass
class JiraIssueIntakeRequest:
    issue_key: str


@dataclass
class JiraIssueIntakeReport:
    started: int = 0
    duplicates: int = 0
    held: int = 0
    skipped: str = ""


def candidate_issue_jql(issue_key: str, project: str, intake_label: str) -> str:
    from services.integration.jira.intake_read import _candidate_jql

    return f'key = "{issue_key}" AND {_candidate_jql(project, intake_label)}'


@activity.defn
async def intake_jira_issue(request: JiraIssueIntakeRequest) -> JiraIssueIntakeReport:
    from scripts.config import get_settings

    from services import temporal_client
    from services.integration.jira.adapter import build_jira_adapter, project_of
    from services.integration.jira.credentials import build_jira_credential_resolver
    from services.integration.tickets import resolve_ticket_backend_name
    from services.integration.tickets.backend import JIRA
    from services.orchestration.intake_reconciler import IntakeReport, process_issue
    from services.orchestration.intake_runtime import build_intake_context

    if not valid_issue_key(request.issue_key):
        raise ApplicationError(
            f"{request.issue_key!r} is not a Jira issue key",
            type="InvalidIssueKey",
            non_retryable=True,
        )
    settings = get_settings()
    if resolve_ticket_backend_name(settings) != JIRA:
        return JiraIssueIntakeReport(skipped="the tenant takes intake from another ticket backend")
    project = project_of(request.issue_key)
    resolver = build_jira_credential_resolver()
    cred = resolver.resolve(project)
    if cred is None:
        return JiraIssueIntakeReport(skipped=f"project {project} is not configured")
    adapter = build_jira_adapter(project=project, resolver=resolver)
    issues = adapter.search_issues(
        candidate_issue_jql(request.issue_key, project, cred.intake_label)
    )
    if not issues:
        return JiraIssueIntakeReport(skipped="the issue is not an intake candidate")
    client = await temporal_client.connect(settings.temporal_host, settings)
    context = build_intake_context(settings, client=client, gh_resolver=None)
    report = IntakeReport()
    await process_issue(issues[0], project=project, cred=cred, ctx=context, report=report)
    return JiraIssueIntakeReport(
        started=sum(1 for record in report.started if not record.duplicate),
        duplicates=sum(1 for record in report.started if record.duplicate),
        held=len(report.held),
    )
