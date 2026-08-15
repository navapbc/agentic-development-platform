"""Hermetic tests for the Jira intake reconciler over two projects with fake collaborators."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from scripts.config import Settings
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.jira import CLOUD
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.intake_reconciler import IntakeStart, reconcile_intake

_ALLOW = frozenset({"repo-a", "repo-b", "repo-c"})
_PROJECTS = {"ADEV", "ADSER"}


@dataclass(frozen=True)
class _Cred:
    default_repo: str
    auth_mode: str = CLOUD


class _Resolver:
    def __init__(self, mapping: dict[str, _Cred]) -> None:
        self._mapping = mapping

    def resolve(self, project: str) -> _Cred | None:
        return self._mapping.get(project)


class _ReadSource:
    def __init__(self, by_project: dict[str, list[dict]]) -> None:
        self._by_project = by_project

    async def list_candidate_issues(self, project: str) -> list[dict]:
        return [dict(issue) for issue in self._by_project.get(project, [])]


class _Starter:
    def __init__(self) -> None:
        self.starts: list[IntakeStart] = []

    async def start(self, start: IntakeStart) -> str:
        self.starts.append(start)
        return f"wf-{start.issue_key}-{start.repo}"


class _Alerter:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str]] = []

    async def unroutable(self, *, issue_key: str, project: str, reason: str) -> None:
        self.alerts.append((issue_key, project, reason))


class _RecordingStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}

    async def persist(self, candidate) -> TaskSnapshot:
        history = self.by_execution.setdefault(candidate.execution_id, [])
        version = history[-1].version + 1 if history else 1
        snapshot = TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, version),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=version,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )
        history.append(snapshot)
        return snapshot


def _issue(key: str, project: str, *, summary: str = "Add retry", description: str = "") -> dict:
    return {
        "key": key,
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "labels": [],
        },
    }


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.jira_allowed_projects",
        lambda settings=None: set(_PROJECTS),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.allowed_repos",
        lambda settings=None: set(_ALLOW),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.acceptance_field",
        lambda settings=None: "",
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.allowed_projects",
        lambda settings=None: set(_PROJECTS),
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.acceptance_field",
        lambda settings=None: "",
    )


def _run(read_source, starter, alerter, ledger, store, resolver):
    return reconcile_intake(
        read_source=read_source,
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        settings=Settings(_env_file=None),
        jira_resolver=resolver,
        snapshot_store=store,
    )


def _defaults(default_repo: str = "repo-a"):
    resolver = _Resolver({p: _Cred(default_repo=default_repo) for p in _PROJECTS})
    return _Starter(), _Alerter(), InMemorySideEffectLedger(), _RecordingStore(), resolver


async def test_default_repo_starts_one_execution():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert [(s.issue_key, s.repo) for s in starter.starts] == [("ADEV-1", "repo-a")]
    assert len(report.started) == 1
    assert not report.started[0].duplicate
    assert alerter.alerts == []


async def test_marker_fans_out_to_each_repo():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV", description="repos: repo-a, repo-b")]}
    )
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(read, starter, alerter, ledger, store, resolver)
    assert {(s.issue_key, s.repo) for s in starter.starts} == {
        ("ADEV-1", "repo-a"),
        ("ADEV-1", "repo-b"),
    }


async def test_scan_match_selects_named_repo():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", description="Land it in repo-c.")]})
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(read, starter, alerter, ledger, store, resolver)
    assert [(s.issue_key, s.repo) for s in starter.starts] == [("ADEV-1", "repo-c")]


async def test_unchanged_repoll_starts_nothing():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(read, starter, alerter, ledger, store, resolver)
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert len(starter.starts) == 1
    assert report.started[0].duplicate


async def test_material_change_starts_new_execution():
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(_ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
         starter, alerter, ledger, store, resolver)
    await _run(_ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
         starter, alerter, ledger, store, resolver)
    assert len(starter.starts) == 2


async def test_unlisted_repo_holds_and_alerts_once():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV", description="repos: repo-a, ghost")]}
    )
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(read, starter, alerter, ledger, store, resolver)
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert starter.starts == []
    assert len(alerter.alerts) == 1
    assert report.held[0].issue_key == "ADEV-1"
    assert not report.held[0].alerted


async def test_zero_repo_holds_and_alerts():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults(default_repo="")
    await _run(read, starter, alerter, ledger, store, resolver)
    assert starter.starts == []
    assert len(alerter.alerts) == 1


async def test_two_projects_each_start_against_their_own_default():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV")], "ADSER": [_issue("ADSER-9", "ADSER")]}
    )
    resolver = _Resolver({"ADEV": _Cred("repo-a"), "ADSER": _Cred("repo-b")})
    starter, alerter = _Starter(), _Alerter()
    ledger, store = InMemorySideEffectLedger(), _RecordingStore()
    await _run(read, starter, alerter, ledger, store, resolver)
    assert {(s.issue_key, s.repo) for s in starter.starts} == {
        ("ADEV-1", "repo-a"),
        ("ADSER-9", "repo-b"),
    }
