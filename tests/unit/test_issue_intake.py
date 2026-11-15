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
        lambda settings: _Resolver(set(projects)),
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
