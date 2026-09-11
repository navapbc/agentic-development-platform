"""Hermetic tests for the Jira intake reconciler over two projects with fake collaborators."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.jira import CLOUD
from services.integration.jira.credentials import ComponentRepos
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.intake_reconciler import IntakeStart, reconcile_intake
from tests.support import hermetic_settings

_ALLOW = frozenset({"repo-a", "repo-b", "repo-c"})
_PROJECTS = {"ADEV", "ADSER"}


@dataclass(frozen=True)
class _Cred:
    default_repo: str
    auth_mode: str = CLOUD
    component_repos: ComponentRepos = field(default_factory=ComponentRepos)


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


class _Seeder:
    def __init__(self) -> None:
        self.seeded: list[tuple[str, str, str]] = []

    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        self.seeded.append((execution_id, task_id, repo))


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


def _issue(
    key: str,
    project: str,
    *,
    summary: str = "Add retry",
    description: str = "",
    components: tuple[str, ...] = (),
) -> dict:
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
            "components": [{"name": name} for name in components],
        },
    }


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.jira_allowed_projects",
        lambda settings=None: set(_PROJECTS),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.source_control_repos",
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


class _ActiveGuard:
    def __init__(self, active: dict[tuple[str, str], str] | None = None) -> None:
        self.active = active or {}
        self.calls: list[tuple[str, str]] = []

    async def active_workflow(self, *, issue_key: str, repo: str) -> str | None:
        self.calls.append((issue_key, repo))
        return self.active.get((issue_key, repo))


def _run(read_source, starter, alerter, ledger, store, resolver, seeder=None, active_guard=None):
    return reconcile_intake(
        read_source=read_source,
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        execution_seeder=seeder or _Seeder(),
        settings=hermetic_settings(),
        jira_resolver=resolver,
        snapshot_store=store,
        active_guard=active_guard,
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


async def test_intake_threads_the_snapshot_version_to_the_starter():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(read, starter, alerter, ledger, store, resolver)
    assert starter.starts[0].snapshot_version == 1



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


async def test_components_fan_out_to_the_mapped_repositories():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV", components=("Backend", "Frontend"))]}
    )
    components = ComponentRepos((("backend", ("repo-a",)), ("frontend", ("repo-b",))))
    resolver = _Resolver({"ADEV": _Cred("repo-c", component_repos=components)})
    starter, alerter = _Starter(), _Alerter()
    ledger, store = InMemorySideEffectLedger(), _RecordingStore()
    await _run(read, starter, alerter, ledger, store, resolver)
    assert {(s.issue_key, s.repo) for s in starter.starts} == {
        ("ADEV-1", "repo-a"),
        ("ADEV-1", "repo-b"),
    }


async def test_component_mapping_outside_the_allowlist_holds_and_alerts():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", components=("Backend",))]})
    components = ComponentRepos((("backend", ("ghost-repo",)),))
    resolver = _Resolver({"ADEV": _Cred("repo-a", component_repos=components)})
    starter, alerter = _Starter(), _Alerter()
    ledger, store = InMemorySideEffectLedger(), _RecordingStore()
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert starter.starts == []
    assert len(alerter.alerts) == 1
    assert report.held[0].issue_key == "ADEV-1"


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


async def test_execution_is_seeded_before_the_workflow_starts():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", description="repos: repo-a, repo-b")]})
    starter, alerter, ledger, store, resolver = _defaults()
    seeder = _Seeder()
    await _run(read, starter, alerter, ledger, store, resolver, seeder)
    assert {(task_id, repo) for _, task_id, repo in seeder.seeded} == {
        ("ADEV-1", "repo-a"),
        ("ADEV-1", "repo-b"),
    }
    seeded_ids = {execution_id for execution_id, _, _ in seeder.seeded}
    started_ids = {s.execution_id for s in starter.starts}
    assert started_ids == seeded_ids


async def test_active_execution_suppresses_start_and_records_duplicate():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    seeder = _Seeder()
    guard = _ActiveGuard({("ADEV-1", "repo-a"): "wf-ADEV-1-repo-a"})
    report = await _run(read, starter, alerter, ledger, store, resolver, seeder, active_guard=guard)
    assert starter.starts == []
    assert seeder.seeded == []
    assert len(report.started) == 1
    assert report.started[0].duplicate
    assert report.started[0].workflow_id == "wf-ADEV-1-repo-a"


async def test_material_change_is_suppressed_while_execution_is_active():
    starter, alerter, ledger, store, resolver = _defaults()
    guard = _ActiveGuard()
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter, alerter, ledger, store, resolver, active_guard=guard,
    )
    guard.active[("ADEV-1", "repo-a")] = "wf-ADEV-1-repo-a"
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter, alerter, ledger, store, resolver, active_guard=guard,
    )
    assert len(starter.starts) == 1


async def test_material_change_starts_again_after_execution_terminates():
    starter, alerter, ledger, store, resolver = _defaults()
    guard = _ActiveGuard()
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter, alerter, ledger, store, resolver, active_guard=guard,
    )
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter, alerter, ledger, store, resolver, active_guard=guard,
    )
    assert len(starter.starts) == 2



class _RunCounter:
    def __init__(self, active: int) -> None:
        self._active = active

    async def count_active_runs(self) -> int:
        return self._active


async def test_admission_caps_new_starts_and_defers_the_rest():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV"), _issue("ADEV-2", "ADEV")], "ADSER": []}
    )
    starter, alerter, ledger, store, resolver = _defaults()
    report = await reconcile_intake(
        read_source=read,
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        execution_seeder=_Seeder(),
        settings=hermetic_settings(max_concurrent_runs=1),
        jira_resolver=resolver,
        snapshot_store=store,
        run_counter=_RunCounter(0),
    )
    assert len(starter.starts) == 1
    assert len(report.deferred) == 1


async def test_admission_defers_all_when_already_at_capacity():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")], "ADSER": []})
    starter, alerter, ledger, store, resolver = _defaults()
    report = await reconcile_intake(
        read_source=read,
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        execution_seeder=_Seeder(),
        settings=hermetic_settings(max_concurrent_runs=4),
        jira_resolver=resolver,
        snapshot_store=store,
        run_counter=_RunCounter(4),
    )
    assert starter.starts == []
    assert report.deferred == ["ADEV-1"]


async def test_no_counter_means_unbounded():
    read = _ReadSource(
        {"ADEV": [_issue("ADEV-1", "ADEV"), _issue("ADEV-2", "ADEV")], "ADSER": []}
    )
    starter, alerter, ledger, store, resolver = _defaults()
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert len(starter.starts) == 2
    assert report.deferred == []
