from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from services.orchestration.temporal_activities.issue_intake import (
    JiraIssueIntakeReport,
    JiraIssueIntakeRequest,
    candidate_issue_jql,
    intake_jira_issue,
    valid_issue_key,
)
from temporalio.exceptions import ApplicationError
from tests.support import hermetic_settings


@dataclass
class _Cred:
    intake_label: str = "agent-ready"


@dataclass
class _Resolver:
    projects: set[str]

    def resolve(self, project: str) -> _Cred | None:
        return _Cred() if project in self.projects else None


@dataclass
class _Adapter:
    issues: list[dict[str, Any]]
    queries: list[str] = field(default_factory=list)

    def search_issues(self, jql: str) -> list[dict[str, Any]]:
        self.queries.append(jql)
        return self.issues


@dataclass
class _Started:
    duplicate: bool


def _wire(monkeypatch, *, backend: str = "jira", issues=None, projects=("BETA",)):
    adapter = _Adapter(issues if issues is not None else [{"key": "BETA-12"}])
    processed: list[str] = []

    async def _connect(host, settings):
        return object()

    async def _process_issue(issue, *, project, cred, ctx, report):
        processed.append(issue["key"])
        report.started.append(_Started(duplicate=False))

    monkeypatch.setattr("scripts.config.get_settings", hermetic_settings)
    monkeypatch.setattr(
        "services.integration.tickets.resolve_ticket_backend_name", lambda settings: backend
    )
    monkeypatch.setattr(
        "services.integration.jira.credentials.build_jira_credential_resolver",
        lambda: _Resolver(set(projects)),
    )
    monkeypatch.setattr(
        "services.integration.jira.adapter.build_jira_adapter", lambda *a, **k: adapter
    )
    monkeypatch.setattr("services.temporal_client.connect", _connect)
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.build_intake_context", lambda *a, **k: object()
    )
    monkeypatch.setattr("services.orchestration.intake_reconciler.process_issue", _process_issue)
    return adapter, processed


def test_issue_keys_are_validated():
    assert valid_issue_key("BETA-12")
    assert not valid_issue_key("beta-12")
    assert not valid_issue_key('BETA-1" OR project = "ADP')


def test_the_candidate_query_narrows_the_poll_query_to_the_key():
    jql = candidate_issue_jql("BETA-12", "BETA", "agent-ready")

    assert jql.startswith('key = "BETA-12" AND project = "BETA"')
    assert 'labels = "agent-ready"' in jql


async def test_a_malformed_key_fails_without_retry():
    with pytest.raises(ApplicationError) as excinfo:
        await intake_jira_issue(JiraIssueIntakeRequest(issue_key="nope"))

    assert excinfo.value.non_retryable


async def test_a_candidate_issue_enters_intake_with_the_tenant_credentials(monkeypatch):
    adapter, processed = _wire(monkeypatch)

    report = await intake_jira_issue(JiraIssueIntakeRequest(issue_key="BETA-12"))

    assert report == JiraIssueIntakeReport(started=1)
    assert processed == ["BETA-12"]
    assert adapter.queries[0].startswith('key = "BETA-12"')


async def test_a_tenant_on_another_ticket_backend_skips_the_issue(monkeypatch):
    _, processed = _wire(monkeypatch, backend="rebar")

    report = await intake_jira_issue(JiraIssueIntakeRequest(issue_key="BETA-12"))

    assert report.skipped
    assert processed == []


async def test_an_unconfigured_project_skips_the_issue(monkeypatch):
    _, processed = _wire(monkeypatch, projects=())

    report = await intake_jira_issue(JiraIssueIntakeRequest(issue_key="BETA-12"))

    assert "not configured" in report.skipped
    assert processed == []


async def test_an_issue_the_poll_would_not_take_is_skipped(monkeypatch):
    _, processed = _wire(monkeypatch, issues=[])

    report = await intake_jira_issue(JiraIssueIntakeRequest(issue_key="BETA-12"))

    assert "not an intake candidate" in report.skipped
    assert processed == []


async def _run_intake_workflow(*activities) -> Any:
    import asyncio
    import uuid

    from services.orchestration.temporal_workflows.issue_intake import (
        JiraIssueIntakeInput,
        JiraIssueIntakeWorkflow,
    )
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    task_queue = f"agent-platform-beta-{uuid.uuid4().hex[:8]}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=task_queue,
            workflows=[JiraIssueIntakeWorkflow],
            activities=list(activities),
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        run = env.client.execute_workflow(
            JiraIssueIntakeWorkflow.run,
            JiraIssueIntakeInput(issue_key="BETA-12"),
            id=f"jira-intake-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        return await asyncio.wait_for(run, timeout=60)


async def test_an_intake_no_activity_worker_takes_fails_the_workflow_naming_its_queue(
    monkeypatch,
):
    from datetime import timedelta

    from services.orchestration.temporal_workflows import issue_intake
    from temporalio.client import WorkflowFailureError

    monkeypatch.setattr(issue_intake, "INTAKE_TIMEOUT", timedelta(seconds=1))

    with pytest.raises(WorkflowFailureError) as failed:
        await _run_intake_workflow()

    cause = failed.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == issue_intake.INTAKE_NOT_STARTED
    assert cause.non_retryable is True
    assert "BETA-12" in cause.message
    assert "agent-platform-beta-" in cause.message


async def test_an_intake_an_activity_worker_takes_completes():
    from temporalio import activity

    @activity.defn(name="intake_jira_issue")
    async def taken(req: JiraIssueIntakeRequest) -> JiraIssueIntakeReport:
        return JiraIssueIntakeReport(started=1, skipped=req.issue_key)

    report = await _run_intake_workflow(taken)

    assert report == JiraIssueIntakeReport(started=1, skipped="BETA-12")
