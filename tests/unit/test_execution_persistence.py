"""Unit tests for the execution row upsert and the active-execution query contract."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from services.orchestration.intake_runtime import PostgresExecutionSeeder
from services.orchestration.temporal_activities.persistence import (
    ExecutionState,
    execution_state_upsert,
)
from sqlalchemy.dialects import postgresql


def _compiled_upsert() -> str:
    payload = ExecutionState(
        execution_id="intake-abc",
        task_id="T-1",
        state="PR_CREATED",
        workflow_id="wf-1",
    )
    return str(execution_state_upsert(payload).compile(dialect=postgresql.dialect())).lower()


def test_upsert_refreshes_updated_at():
    sql = _compiled_upsert()
    do_update = sql.split("do update set", 1)[1]
    assert "updated_at" in do_update
    assert "now()" in do_update


def test_upsert_still_updates_state_and_workflow():
    do_update = _compiled_upsert().split("do update set", 1)[1]
    assert "state" in do_update
    assert "workflow_id" in do_update
    assert "resolved_models" in do_update


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, connection):
        self._connection = connection

    def connect(self):
        return self._connection


class _FakeScalarResult(_FakeResult):
    def __init__(self, value):
        super().__init__([])
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeCountConnection(_FakeConnection):
    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeScalarResult(0)


def _seeder_with_rows(rows):
    seeder = PostgresExecutionSeeder()
    connection = _FakeConnection(rows)
    seeder._get_engine = lambda: _FakeEngine(connection)  # type: ignore[method-assign]
    return seeder, connection


@pytest.mark.asyncio
async def test_active_query_selects_state():
    row = SimpleNamespace(
        execution_id="intake-abc",
        task_id="T-1",
        workflow_id="wf-1",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        state="PR_CREATED",
    )
    seeder, connection = _seeder_with_rows([row])

    active = await seeder.list_active_executions()

    assert "state" in str(connection.statements[0]).lower()
    assert [execution.state for execution in active] == ["PR_CREATED"]


@pytest.mark.asyncio
async def test_active_query_tolerates_null_state():
    row = SimpleNamespace(
        execution_id="intake-abc",
        task_id="T-1",
        workflow_id=None,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        state=None,
    )
    seeder, _ = _seeder_with_rows([row])

    active = await seeder.list_active_executions()

    assert active[0].state == ""
    assert active[0].workflow_id == ""


@pytest.mark.asyncio
async def test_active_count_excludes_completed_executions():
    seeder = PostgresExecutionSeeder()
    connection = _FakeCountConnection([])
    seeder._get_engine = lambda: _FakeEngine(connection)  # type: ignore[method-assign]

    active_count = await seeder.count_active_runs()

    sql = str(
        connection.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert active_count == 0
    assert "state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')" in sql
