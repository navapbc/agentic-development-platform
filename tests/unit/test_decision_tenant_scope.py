"""A tenant's gate, repair, and restart decisions reach only that tenant's parked runs.

Every tenant's workflows share one Temporal namespace, so an issue key that two tenants both
track names a parked run in each. The decision stores read the platform executions table, and the
tenant scope is what keeps a decision from signalling the other tenant's run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from database.models.schema import executions
from scripts.config import Settings
from services.orchestration import gate_decision, repair_decision, task_restart
from services.orchestration.intake_retry import WorkflowLiveness
from sqlalchemy import Engine, create_engine, event, insert

ISSUE = "PROJ-12"
PARKED = {"alpha": "wf-alpha", "beta": "wf-beta", None: "wf-untenanted", "": "wf-empty"}


class _SqliteEngine:
    """Run the stores' statements against an in-memory executions table."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def connect(self):
        with self._engine.connect() as conn:
            yield _Connection(conn)

    async def dispose(self) -> None:
        return None


class _Connection:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, statement):
        return self._conn.execute(statement)


@pytest.fixture
def engine() -> Iterator[_SqliteEngine]:
    sync = create_engine("sqlite://")
    event.listen(
        sync,
        "connect",
        lambda conn, _: conn.execute("ATTACH DATABASE ':memory:' AS agent_platform"),
    )
    with sync.begin() as conn:
        executions.create(conn)
        for tenant, workflow_id in PARKED.items():
            conn.execute(
                insert(executions).values(
                    execution_id=workflow_id,
                    task_id=ISSUE,
                    state="AWAITING_HUMAN",
                    workflow_id=workflow_id,
                    tenant=tenant,
                )
            )
        conn.execute(
            insert(executions).values(
                execution_id="wf-alpha-running",
                task_id=ISSUE,
                state="IMPLEMENTING",
                workflow_id="wf-alpha-running",
                tenant="alpha",
            )
        )
    yield _SqliteEngine(sync)
    sync.dispose()


class _Signaller:
    def __init__(self, _settings: Any = None) -> None:
        self.signalled: list[str] = []
        _SENT.append(self)

    async def signal_gate_decision(
        self, workflow_id: str, *, gate: str, approved: bool, actor: str
    ) -> None:
        self.signalled.append(workflow_id)

    async def signal_repair_decision(self, workflow_id: str, *, retry: bool, actor: str) -> None:
        self.signalled.append(workflow_id)


_SENT: list[_Signaller] = []


@pytest.fixture
def platform(engine, monkeypatch) -> Iterator[None]:
    _SENT.clear()
    for module in (gate_decision, repair_decision, task_restart):
        monkeypatch.setattr(module, "create_platform_engine", lambda _url: engine)
    monkeypatch.setattr(gate_decision, "TemporalGateSignaller", _Signaller)
    monkeypatch.setattr(repair_decision, "TemporalRepairSignaller", _Signaller)
    monkeypatch.setattr(task_restart, "TemporalRepairSignaller", _Signaller)
    yield
    _SENT.clear()


@pytest.mark.parametrize(
    ("tenant", "expected"),
    [("alpha", ["wf-alpha"]), ("beta", ["wf-beta"]), ("", ["wf-empty", "wf-untenanted"])],
)
async def test_a_gate_decision_reaches_only_the_tenants_parked_run(platform, tenant, expected):
    result = await gate_decision.run_gate_decision(
        ISSUE, gate="plan", approved=True, settings=Settings(tenant=tenant)
    )

    assert sorted(result.signalled) == expected
    assert sorted(_SENT[0].signalled) == expected


@pytest.mark.parametrize(
    ("tenant", "expected"),
    [("alpha", ["wf-alpha"]), ("beta", ["wf-beta"]), ("", ["wf-empty", "wf-untenanted"])],
)
async def test_a_repair_decision_reaches_only_the_tenants_parked_run(platform, tenant, expected):
    result = await repair_decision.run_repair_decision(
        ISSUE, retry=True, settings=Settings(tenant=tenant)
    )

    assert sorted(result.signalled) == expected
    assert sorted(_SENT[0].signalled) == expected


class _IntakeStore:
    def __init__(self, _engine: Any, *, tenant: str) -> None:
        self.tenant = tenant

    async def workflow_ids(self, issue_key: str) -> list[str]:
        return []

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int:
        return 1

    async def mark_non_terminal_failed(self, issue_key: str) -> int:
        return 0

    async def clear_sequence_gate(self, issue_key: str) -> int:
        return 0

    async def clear_work_claims(self, issue_key: str) -> int:
        return 0


async def test_a_restart_abandons_only_the_tenants_parked_run(platform, monkeypatch):
    async def ended(_workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.ENDED

    monkeypatch.setattr(task_restart, "PostgresIntakeRetryStore", _IntakeStore)
    monkeypatch.setattr(task_restart, "temporal_workflow_liveness", lambda _settings: ended)
    monkeypatch.setattr(task_restart, "build_intake_cursor_release", lambda _settings: None)

    result = await task_restart.run_restart_task(ISSUE, settings=Settings(tenant="alpha"))

    assert result.abandoned == ("wf-alpha",)
    assert _SENT[0].signalled == ["wf-alpha"]
