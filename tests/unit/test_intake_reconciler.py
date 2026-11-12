"""Hermetic tests for the Jira intake reconciler over two projects with fake collaborators."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from services.coordination.idempotency import InMemorySideEffectLedger, SideEffectRef
from services.integration.jira import CLOUD
from services.integration.jira.credentials import ComponentRepos
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.checks.config import required_checks_repos
from services.orchestration.intake_base import UnresolvableBaseBranch
from services.orchestration.intake_reconciler import (
    IntakeStart,
    candidate_from_rebar,
    checks_not_configured_reason,
    reconcile_intake,
)
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
    labels: tuple[str, ...] = (),
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
            "labels": list(labels),
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


class _RecordingLedger(InMemorySideEffectLedger):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[SideEffectRef] = []

    async def reserve(self, ref: SideEffectRef):
        self.refs.append(ref)
        return await super().reserve(ref)


def _run(
    read_source,
    starter,
    alerter,
    ledger,
    store,
    resolver,
    seeder=None,
    active_guard=None,
    base_resolver=None,
    checks_repos=None,
):
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
        base_resolver=base_resolver,
        checks_repos=checks_repos,
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
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", description="repos: repo-a, repo-b")]})
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
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", components=("Backend", "Frontend"))]})
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


async def test_intake_start_dedup_key_uses_ticket_identity_and_repo():
    starter, alerter, ledger, store, resolver = (
        _Starter(),
        _Alerter(),
        _RecordingLedger(),
        _RecordingStore(),
        _Resolver({p: _Cred(default_repo="repo-a") for p in _PROJECTS}),
    )
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
    )
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
    )

    assert [ref.target for ref in ledger.refs if ref.operation == "intake_start"] == [
        "ADEV-1:repo-a",
        "ADEV-1:repo-a",
    ]


async def test_ticket_writeback_change_after_terminal_execution_is_deduplicated():
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
    )
    report = await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
    )
    assert len(starter.starts) == 1
    assert report.started[0].duplicate


async def test_cleared_intake_dedup_allows_operator_requested_rerun():
    starter, alerter, ledger, store, resolver = _defaults()
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
    )
    report = await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter,
        alerter,
        InMemorySideEffectLedger(),
        store,
        resolver,
    )
    assert len(starter.starts) == 2
    assert not report.started[0].duplicate


async def test_unlisted_repo_holds_and_alerts_once():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", description="repos: repo-a, ghost")]})
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
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")], "ADSER": [_issue("ADSER-9", "ADSER")]})
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
        starter,
        alerter,
        ledger,
        store,
        resolver,
        active_guard=guard,
    )
    guard.active[("ADEV-1", "repo-a")] = "wf-ADEV-1-repo-a"
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
        active_guard=guard,
    )
    assert len(starter.starts) == 1


async def test_ticket_writeback_change_stays_deduplicated_after_execution_terminates():
    starter, alerter, ledger, store, resolver = _defaults()
    guard = _ActiveGuard()
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v1")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
        active_guard=guard,
    )
    await _run(
        _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", summary="v2")]}),
        starter,
        alerter,
        ledger,
        store,
        resolver,
        active_guard=guard,
    )
    assert len(starter.starts) == 1


class _RunCounter:
    def __init__(self, active: int) -> None:
        self._active = active

    async def count_active_runs(self) -> int:
        return self._active


async def test_admission_caps_new_starts_and_defers_the_rest():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV"), _issue("ADEV-2", "ADEV")], "ADSER": []})
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
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV"), _issue("ADEV-2", "ADEV")], "ADSER": []})
    starter, alerter, ledger, store, resolver = _defaults()
    report = await _run(read, starter, alerter, ledger, store, resolver)
    assert len(starter.starts) == 2
    assert report.deferred == []


class _UnresolvableBaseStarter(_Starter):
    def __init__(self, missing: str) -> None:
        super().__init__()
        self._missing = missing

    async def start(self, start: IntakeStart) -> str:
        if start.base == self._missing:
            raise UnresolvableBaseBranch(start.base, start.repo)
        return await super().start(start)


async def test_declared_base_label_reaches_the_starter():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:feature-x",))]})
    starter, alerter, ledger, store, resolver = _defaults()

    await _run(read, starter, alerter, ledger, store, resolver)

    assert [s.base for s in starter.starts] == ["feature-x"]


async def test_no_base_label_leaves_the_base_unset():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()

    await _run(read, starter, alerter, ledger, store, resolver)

    assert [s.base for s in starter.starts] == [""]


async def test_conflicting_base_labels_start_no_workflow():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:x", "base:y"))]})
    starter, alerter, ledger, store, resolver = _defaults()

    report = await _run(read, starter, alerter, ledger, store, resolver)

    assert starter.starts == []
    assert report.started == []


async def test_conflicting_base_labels_record_a_hold_naming_both():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:x", "base:y"))]})
    starter, alerter, ledger, store, resolver = _defaults()

    report = await _run(read, starter, alerter, ledger, store, resolver)

    assert [h.issue_key for h in report.held] == ["ADEV-1"]
    reason = report.held[0].reason
    assert "x" in reason and "y" in reason
    assert [a[0] for a in alerter.alerts] == ["ADEV-1"]


async def test_unresolvable_declared_base_starts_no_workflow():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:gone",))]})
    _, alerter, ledger, store, resolver = _defaults()
    starter = _UnresolvableBaseStarter("gone")

    report = await _run(read, starter, alerter, ledger, store, resolver)

    assert starter.starts == []
    assert report.started == []


async def test_unresolvable_declared_base_records_a_hold_naming_the_branch():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:gone",))]})
    _, alerter, ledger, store, resolver = _defaults()
    starter = _UnresolvableBaseStarter("gone")

    report = await _run(read, starter, alerter, ledger, store, resolver)

    assert [h.issue_key for h in report.held] == ["ADEV-1"]
    assert "gone" in report.held[0].reason


async def test_a_held_ticket_does_not_stop_the_pass():
    read = _ReadSource(
        {
            "ADEV": [
                _issue("ADEV-1", "ADEV", labels=("base:gone",)),
                _issue("ADEV-2", "ADEV"),
            ]
        }
    )
    _, alerter, ledger, store, resolver = _defaults()
    starter = _UnresolvableBaseStarter("gone")

    report = await _run(read, starter, alerter, ledger, store, resolver)

    assert [s.issue_key for s in starter.starts] == ["ADEV-2"]
    assert [h.issue_key for h in report.held] == ["ADEV-1"]


class _BaseResolver:
    def __init__(self, known: dict[str, str]) -> None:
        self.known = known
        self.calls: list[tuple[str, str]] = []

    async def resolve_base(self, *, repo: str, branch: str) -> str:
        self.calls.append((repo, branch))
        return self.known.get(branch, "")


async def test_unresolvable_declared_base_seeds_no_execution():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:gone",))]})
    starter, alerter, ledger, store, resolver = _defaults()
    seeder = _Seeder()
    base_resolver = _BaseResolver({})

    report = await _run(
        read, starter, alerter, ledger, store, resolver, seeder=seeder, base_resolver=base_resolver
    )

    assert seeder.seeded == []
    assert store.by_execution == {}
    assert starter.starts == []
    assert [h.issue_key for h in report.held] == ["ADEV-1"]


async def test_unresolvable_declared_base_holds_once_per_pass_without_growth():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:gone",))]})
    starter, alerter, ledger, store, resolver = _defaults()
    seeder = _Seeder()
    base_resolver = _BaseResolver({})

    for _ in range(3):
        await _run(
            read,
            starter,
            alerter,
            ledger,
            store,
            resolver,
            seeder=seeder,
            base_resolver=base_resolver,
        )

    assert seeder.seeded == []
    assert starter.starts == []


async def test_resolvable_declared_base_starts_the_workflow():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:feature-x",))]})
    starter, alerter, ledger, store, resolver = _defaults()
    seeder = _Seeder()
    base_resolver = _BaseResolver({"feature-x": "feature-sha"})

    report = await _run(
        read, starter, alerter, ledger, store, resolver, seeder=seeder, base_resolver=base_resolver
    )

    assert base_resolver.calls == [("repo-a", "feature-x")]
    assert [s.base for s in starter.starts] == ["feature-x"]
    assert len(seeder.seeded) == 1
    assert report.held == []


async def test_undeclared_base_skips_the_resolver():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    base_resolver = _BaseResolver({})

    report = await _run(
        read, starter, alerter, ledger, store, resolver, base_resolver=base_resolver
    )

    assert base_resolver.calls == []
    assert [s.base for s in starter.starts] == [""]
    assert report.held == []


async def test_declared_base_without_a_resolver_still_holds_every_pass():
    # Growth of seeder.seeded is not asserted here because this path re-seeds by design gap,
    # tracked as 855e-f65d-ee67-4ec7.
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV", labels=("base:gone",))]})
    _, alerter, ledger, store, resolver = _defaults()
    starter = _UnresolvableBaseStarter("gone")
    seeder = _Seeder()

    held = []
    for _ in range(3):
        report = await _run(read, starter, alerter, ledger, store, resolver, seeder=seeder)
        held.append([h.issue_key for h in report.held])

    assert held == [["ADEV-1"], ["ADEV-1"], ["ADEV-1"]]
    assert starter.starts == []


def test_rebar_candidate_carries_the_declared_file_impact() -> None:
    ticket = {
        "ticket_id": "aaaa-bbbb",
        "title": "Remove the size prop",
        "description": "body",
        "status": "open",
        "file_impact": [
            {"path": "portal/src/page.tsx", "reason": "drop the prop"},
            {"path": "portal/tests/page.test.ts", "reason": "refresh the assertion"},
        ],
    }

    candidate = candidate_from_rebar(ticket, project="PROJ", default_repo="repo-a")

    assert candidate.file_impact == ("portal/src/page.tsx", "portal/tests/page.test.ts")


def test_rebar_candidate_file_impact_is_empty_when_undeclared() -> None:
    ticket = {
        "ticket_id": "aaaa-bbbb",
        "title": "Remove the size prop",
        "description": "body",
        "status": "open",
        "file_impact": [],
    }

    candidate = candidate_from_rebar(ticket, project="PROJ", default_repo="repo-a")

    assert candidate.file_impact == ()


async def test_holds_a_repository_without_checks_when_ci_is_required():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()

    report = await _run(read, starter, alerter, ledger, store, resolver, checks_repos=frozenset())

    assert starter.starts == []
    assert [h.reason for h in report.held] == [checks_not_configured_reason(["repo-a"])]
    assert alerter.alerts == [("ADEV-1", "ADEV", checks_not_configured_reason(["repo-a"]))]
    assert report.deferred == ["ADEV-1"]


async def test_starts_a_repository_without_checks_when_ci_is_not_required():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()
    local = hermetic_settings(source_control_backend="local")

    report = await _run(
        read, starter, alerter, ledger, store, resolver, checks_repos=required_checks_repos(local)
    )

    assert [(s.issue_key, s.repo) for s in starter.starts] == [("ADEV-1", "repo-a")]
    assert report.held == []


async def test_starts_a_repository_with_checks_when_ci_is_required():
    read = _ReadSource({"ADEV": [_issue("ADEV-1", "ADEV")]})
    starter, alerter, ledger, store, resolver = _defaults()

    report = await _run(
        read, starter, alerter, ledger, store, resolver, checks_repos=frozenset({"repo-a"})
    )

    assert [(s.issue_key, s.repo) for s in starter.starts] == [("ADEV-1", "repo-a")]
    assert report.held == []


def test_required_checks_repos_lists_configured_repositories_under_github(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.checks.config.load_checks_configs",
        lambda settings=None, *, environment=None: {"repo-a": object()},
    )

    assert required_checks_repos(hermetic_settings()) == frozenset({"repo-a"})
