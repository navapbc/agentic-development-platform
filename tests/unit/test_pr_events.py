from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from services.orchestration.webhook.pr_events import PostgresWorkflowResolver, PrClosedEvent
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(self._row)


class _FakeEngine:
    def __init__(self, connection):
        self._connection = connection

    def connect(self):
        return self._connection


def _event() -> PrClosedEvent:
    return PrClosedEvent(
        repo="agentic-demo-service",
        number=5,
        branch="agent/T-1",
        task_id="T-1",
        merged=True,
    )


def _compiled(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_resolver_ignores_terminal_execution_rows():
    connection = _FakeConnection(None)
    resolver = PostgresWorkflowResolver(cast(AsyncEngine, _FakeEngine(connection)))

    workflow_id = await resolver.resolve(_event())

    sql = _compiled(connection.statements[0])
    assert workflow_id is None
    assert "state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')" in sql


@pytest.mark.asyncio
async def test_resolver_prioritizes_awaiting_pr_close_workflow():
    connection = _FakeConnection(SimpleNamespace(workflow_id="wf-awaiting-close"))
    resolver = PostgresWorkflowResolver(cast(AsyncEngine, _FakeEngine(connection)))

    workflow_id = await resolver.resolve(_event())

    sql = _compiled(connection.statements[0])
    assert workflow_id == "wf-awaiting-close"
    assert "state = 'AWAITING_PR_CLOSE' DESC" in sql
