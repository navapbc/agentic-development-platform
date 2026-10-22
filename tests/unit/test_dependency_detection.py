"""U1: planning-phase dependency detection over file impact and unresolved symbols."""

from __future__ import annotations

import pytest
from services.orchestration.dependency_detection import (
    FILE_OVERLAP,
    UNRESOLVED_SYMBOL,
    PeerRun,
    detect_dependency,
    predicted_touch_set,
    unresolved_symbol_files,
)


def _package(*, candidate_files=(), symbols=(), references=()) -> dict:
    return {
        "candidate_files": list(candidate_files),
        "symbols": list(symbols),
        "references": list(references),
    }


def _symbol(name: str, path: str) -> dict:
    return {"name": name, "location": {"path": path, "start_line": 1}}


def _reference(name: str, path: str, *, is_definition: bool) -> dict:
    return {
        "symbol": name,
        "is_definition": is_definition,
        "location": {"path": path, "start_line": 2},
    }


def test_predicted_touch_set_unions_candidates_symbols_and_declared():
    package = _package(
        candidate_files=["portal/src/a.ts"],
        symbols=[_symbol("Foo", "portal/src/foo.ts")],
    )
    assert predicted_touch_set(package, ["api/models/x.py"]) == (
        "api/models/x.py",
        "portal/src/a.ts",
        "portal/src/foo.ts",
    )


def test_unresolved_symbol_files_lists_references_undefined_at_base():
    package = _package(
        symbols=[_symbol("Defined", "portal/src/defined.ts")],
        references=[
            _reference("Defined", "portal/src/uses_defined.ts", is_definition=False),
            _reference("Missing", "portal/src/uses_missing.ts", is_definition=False),
            _reference("Missing", "portal/src/def_missing.ts", is_definition=True),
        ],
    )
    assert unresolved_symbol_files(package) == ("portal/src/uses_missing.ts",)


def test_file_overlap_yields_a_detection():
    peers = [PeerRun("e2", "T2", ("portal/src/a.ts", "portal/src/c.ts"))]
    finding = detect_dependency(
        touch_set=["portal/src/a.ts", "portal/src/b.ts"], symbol_files=[], peers=peers
    )
    assert finding is not None
    assert finding.blocking_task_id == "T2"
    assert finding.reason == FILE_OVERLAP
    assert finding.overlap == ("portal/src/a.ts",)


def test_unresolved_symbol_yields_a_detection():
    peers = [PeerRun("e2", "T2", ("portal/src/needs.ts",))]
    finding = detect_dependency(
        touch_set=["portal/src/unrelated.ts"],
        symbol_files=["portal/src/needs.ts"],
        peers=peers,
    )
    assert finding is not None
    assert finding.reason == UNRESOLVED_SYMBOL
    assert finding.blocking_task_id == "T2"


def test_no_overlap_yields_none():
    peers = [PeerRun("e2", "T2", ("services/other.py",))]
    assert (
        detect_dependency(
            touch_set=["portal/src/a.ts"], symbol_files=["portal/src/b.ts"], peers=peers
        )
        is None
    )


def test_no_peers_yields_none():
    assert detect_dependency(touch_set=["a.ts"], symbol_files=["b.ts"], peers=[]) is None


def test_file_overlap_takes_precedence_over_symbol():
    peers = [PeerRun("e2", "T2", ("shared.ts",))]
    finding = detect_dependency(touch_set=["shared.ts"], symbol_files=["shared.ts"], peers=peers)
    assert finding is not None
    assert finding.reason == FILE_OVERLAP


class _CapableBackend:
    def link(self, from_issue: str, to_issue: str, *, relationship: str) -> None: ...

    def dependency_graph_active(self) -> bool:
        return True

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return ()

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        return ()


class _PlainBackend:
    def get_issue(self, issue_key: str) -> dict:
        return {}


class _FakeStore:
    def __init__(self, impacts: dict[str, tuple[str, ...]] | None = None) -> None:
        self.impacts: dict[str, tuple[str, ...]] = dict(impacts or {})

    async def write(self, task_id: str, paths, execution_id: str) -> None:
        self.impacts[task_id] = tuple(paths)

    async def read(self, task_id: str) -> tuple[str, ...]:
        return self.impacts.get(task_id, ())


class _Exec:
    def __init__(self, execution_id: str, task_id: str) -> None:
        self.execution_id = execution_id
        self.task_id = task_id
        self.workflow_id = ""
        self.updated_at = None


def _seeder(execs):
    class _FakeSeeder:
        async def list_active_executions(self):
            return list(execs)

    return _FakeSeeder


def _patch(monkeypatch, backend, execs, store=None):
    from services.orchestration.temporal_activities import dependency as dep

    store = store or _FakeStore()
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key: backend,
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder", _seeder(execs)
    )
    monkeypatch.setattr("services.orchestration.file_impact_store.FileImpactStore", lambda: store)
    return dep, store


async def test_activity_detects_and_publishes(monkeypatch):
    store = _FakeStore(impacts={"T2": ("portal/src/a.ts",)})
    backend = _CapableBackend()
    dep, store = _patch(monkeypatch, backend, [_Exec("e2", "T2")], store=store)
    result = await dep.detect_dependency(
        dep.DetectDependencyRequest(
            repo="archive-massgov-pfml",
            task_id="T1",
            execution_id="e1",
            issue_key="T1",
            context_package=_package(candidate_files=["portal/src/a.ts"]),
        )
    )
    assert result.detected is True
    assert result.active is True
    assert result.blocking_task_id == "T2"
    assert result.reason == FILE_OVERLAP
    assert store.impacts["T1"] == ("portal/src/a.ts",)


async def test_activity_without_capability_is_inactive(monkeypatch):
    dep, _store = _patch(monkeypatch, _PlainBackend(), [])
    result = await dep.detect_dependency(
        dep.DetectDependencyRequest(repo="r", task_id="T1", execution_id="e1", issue_key="T1")
    )
    assert result.detected is False
    assert result.active is False


async def test_activity_excludes_self(monkeypatch):
    store = _FakeStore(impacts={"T1": ("portal/src/a.ts",)})
    backend = _CapableBackend()
    dep, store = _patch(monkeypatch, backend, [_Exec("e1", "T1")], store=store)
    result = await dep.detect_dependency(
        dep.DetectDependencyRequest(
            repo="r",
            task_id="T1",
            execution_id="e1",
            issue_key="T1",
            context_package=_package(candidate_files=["portal/src/a.ts"]),
        )
    )
    assert result.detected is False
    assert result.active is True


class _ProducerBackend:
    def __init__(self, *, status: str, dependents: tuple[str, ...]) -> None:
        self._status = status
        self._dependents = dependents

    def get_status(self, issue_key: str) -> str:
        return self._status

    def link(self, from_issue: str, to_issue: str, *, relationship: str) -> None: ...

    def dependency_graph_active(self) -> bool:
        return True

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return ()

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        return self._dependents


def _patch_producer(monkeypatch, backend, execs, terminal="closed"):
    from services.integration.jira.credentials import StatusFlow
    from services.orchestration.temporal_activities import dependency as dep

    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key: backend,
    )
    monkeypatch.setattr(
        "services.integration.tickets.resolve_status_flow",
        lambda **kwargs: StatusFlow(terminal=terminal),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder", _seeder(execs)
    )
    signaled: list[tuple[str, str, str]] = []

    class _Handle:
        def __init__(self, workflow_id: str) -> None:
            self._workflow_id = workflow_id

        async def signal(self, name: str, arg: str) -> None:
            signaled.append((self._workflow_id, name, arg))

    class _Client:
        def get_workflow_handle(self, workflow_id: str) -> _Handle:
            return _Handle(workflow_id)

    async def _connect(*args, **kwargs) -> _Client:
        return _Client()

    monkeypatch.setattr("temporalio.client.Client.connect", _connect)
    return dep, signaled


class _ExecWf:
    def __init__(self, execution_id: str, task_id: str, workflow_id: str) -> None:
        self.execution_id = execution_id
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.updated_at = None


async def test_signal_ready_signals_active_dependents(monkeypatch):
    from services.orchestration.suspension import RESUME_SIGNAL, ResumeOn

    backend = _ProducerBackend(status="closed", dependents=("D1",))
    execs = [_ExecWf("e-d1", "D1", "wf-d1"), _ExecWf("e-d2", "D2", "wf-d2")]
    dep, signaled = _patch_producer(monkeypatch, backend, execs)
    result = await dep.signal_dependency_ready(
        dep.SignalDependencyReadyRequest(blocking_task_id="B1", blocking_issue_key="B1")
    )
    assert result.signaled == 1
    assert signaled == [("wf-d1", RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)]


async def test_signal_ready_skips_when_blocker_not_terminal(monkeypatch):
    backend = _ProducerBackend(status="in_progress", dependents=("D1",))
    execs = [_ExecWf("e-d1", "D1", "wf-d1")]
    dep, signaled = _patch_producer(monkeypatch, backend, execs)
    result = await dep.signal_dependency_ready(
        dep.SignalDependencyReadyRequest(blocking_task_id="B1", blocking_issue_key="B1")
    )
    assert result.signaled == 0
    assert signaled == []


async def test_signal_ready_with_no_dependents(monkeypatch):
    backend = _ProducerBackend(status="closed", dependents=())
    dep, signaled = _patch_producer(monkeypatch, backend, [])
    result = await dep.signal_dependency_ready(
        dep.SignalDependencyReadyRequest(blocking_task_id="B1", blocking_issue_key="B1")
    )
    assert result.signaled == 0


class _CycleBackend:
    def __init__(self, deps: dict[str, tuple[str, ...]]) -> None:
        self._deps = deps

    def link(self, from_issue: str, to_issue: str, *, relationship: str) -> None: ...

    def dependency_graph_active(self) -> bool:
        return True

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return self._deps.get(issue_key, ())

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        return ()


async def test_cycle_detected_when_blocker_transitively_depends_on_dependent(monkeypatch):
    from services.orchestration.temporal_activities import dependency as dep

    backend = _CycleBackend({"B1": ("M1",), "M1": ("D1",)})
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key: backend,
    )
    result = await dep.check_dependency_cycle(
        dep.CheckDependencyCycleRequest(
            dependent_task_id="D1", blocking_issue_key="B1", blocking_task_id="B1"
        )
    )
    assert result.is_cycle is True


async def test_no_cycle_when_blocker_does_not_reach_dependent(monkeypatch):
    from services.orchestration.temporal_activities import dependency as dep

    backend = _CycleBackend({"B1": ("X9",)})
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key: backend,
    )
    result = await dep.check_dependency_cycle(
        dep.CheckDependencyCycleRequest(
            dependent_task_id="D1", blocking_issue_key="B1", blocking_task_id="B1"
        )
    )
    assert result.is_cycle is False


def test_dependency_config_defaults_are_off():
    from tests.support import hermetic_settings

    settings = hermetic_settings()
    assert settings.dependency_detection_enabled is False
    assert settings.dependency_replan_attempts == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
