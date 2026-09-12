"""The dependency activities run on a Jira backend through the platform store and issue-link graph.

These wire the real ``JiraAdapter`` graph capability through the backend-neutral
detect, producer, and cycle activities with a fake vendor client and a fake
touch-set store, proving the Jira path resolves overlap from the store and the
dependency graph from Jira issue links without any branch on backend.
"""

from __future__ import annotations

from services.integration.jira.adapter import JiraAdapter


class _FakeVendor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.links: dict[str, list[dict]] = {}
        self.status = "open"

    def set_relationship(self, from_key: str, to_key: str, link_type: str = "Blocks") -> dict:
        self.calls.append((from_key, to_key, link_type))
        self.links.setdefault(from_key, []).append(
            {"type": {"name": link_type}, "outwardIssue": {"key": to_key}}
        )
        self.links.setdefault(to_key, []).append(
            {"type": {"name": link_type}, "inwardIssue": {"key": from_key}}
        )
        return {}

    def get_issue_links(self, remote_id: str) -> list[dict]:
        return self.links.get(remote_id, [])

    def get_issue(self, remote_id: str) -> dict:
        return {"key": remote_id, "fields": {"status": {"name": self.status}}}


class _FakeStore:
    def __init__(self, impacts: dict[str, tuple[str, ...]] | None = None) -> None:
        self.impacts: dict[str, tuple[str, ...]] = dict(impacts or {})

    async def write(self, task_id: str, paths, execution_id: str) -> None:
        self.impacts[task_id] = tuple(paths)

    async def read(self, task_id: str) -> tuple[str, ...]:
        return self.impacts.get(task_id, ())


class _Exec:
    def __init__(self, execution_id: str, task_id: str, workflow_id: str = "") -> None:
        self.execution_id = execution_id
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.updated_at = None


def _seeder(execs):
    class _FakeSeeder:
        async def list_active_executions(self):
            return list(execs)

    return _FakeSeeder


def _adapter(vendor: _FakeVendor) -> JiraAdapter:
    return JiraAdapter(
        source="datacenter", client=vendor, project="PROJ", dependency_link_type="Blocks"
    )


async def test_jira_detection_records_and_signals_end_to_end(monkeypatch):
    from services.integration.jira.credentials import StatusFlow
    from services.orchestration.suspension import RESUME_SIGNAL, ResumeOn
    from services.orchestration.temporal_activities import dependency as dep

    vendor = _FakeVendor()
    adapter = _adapter(vendor)
    store = _FakeStore(impacts={"PROJ-2": ("portal/src/a.ts",)})
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue", lambda key: adapter
    )
    monkeypatch.setattr(
        "services.orchestration.file_impact_store.FileImpactStore", lambda: store
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder",
        _seeder([_Exec("e2", "PROJ-2", "wf-2")]),
    )

    detection = await dep.detect_dependency(
        dep.DetectDependencyRequest(
            repo="archive-massgov-pfml",
            task_id="PROJ-1",
            execution_id="e1",
            issue_key="PROJ-1",
            context_package={"candidate_files": ["portal/src/a.ts"]},
        )
    )
    assert detection.detected is True
    assert detection.blocking_task_id == "PROJ-2"
    assert store.impacts["PROJ-1"] == ("portal/src/a.ts",)

    adapter.link("PROJ-1", "PROJ-2", relationship="depends_on")
    assert vendor.calls == [("PROJ-2", "PROJ-1", "Blocks")]
    assert adapter.declared_dependencies("PROJ-1") == ("PROJ-2",)
    assert adapter.dependents("PROJ-2") == ("PROJ-1",)

    monkeypatch.setattr(
        "services.integration.tickets.resolve_status_flow",
        lambda **kwargs: StatusFlow(terminal="closed"),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder",
        _seeder([_Exec("e1", "PROJ-1", "wf-1")]),
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
    vendor.status = "closed"

    result = await dep.signal_dependency_ready(
        dep.SignalDependencyReadyRequest(blocking_task_id="PROJ-2", blocking_issue_key="PROJ-2")
    )
    assert result.signaled == 1
    assert signaled == [("wf-1", RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)]


async def test_jira_cycle_guard_reads_the_issue_link_graph(monkeypatch):
    from services.orchestration.temporal_activities import dependency as dep

    vendor = _FakeVendor()
    adapter = _adapter(vendor)
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue", lambda key: adapter
    )
    adapter.link("PROJ-2", "PROJ-1", relationship="depends_on")

    result = await dep.check_dependency_cycle(
        dep.CheckDependencyCycleRequest(
            dependent_task_id="PROJ-1",
            blocking_issue_key="PROJ-2",
            blocking_task_id="PROJ-2",
        )
    )
    assert result.is_cycle is True


async def test_jira_without_link_type_records_nothing(monkeypatch):
    from services.orchestration.temporal_activities import dependency as dep

    vendor = _FakeVendor()
    adapter = JiraAdapter(
        source="datacenter", client=vendor, project="PROJ", dependency_link_type=""
    )
    store = _FakeStore(impacts={"PROJ-2": ("portal/src/a.ts",)})
    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue", lambda key: adapter
    )
    monkeypatch.setattr(
        "services.orchestration.file_impact_store.FileImpactStore", lambda: store
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder",
        _seeder([_Exec("e2", "PROJ-2", "wf-2")]),
    )

    detection = await dep.detect_dependency(
        dep.DetectDependencyRequest(
            repo="archive-massgov-pfml",
            task_id="PROJ-1",
            execution_id="e1",
            issue_key="PROJ-1",
            context_package={"candidate_files": ["portal/src/a.ts"]},
        )
    )
    assert detection.detected is False
    assert detection.active is False
    assert store.impacts["PROJ-1"] == ("portal/src/a.ts",)
