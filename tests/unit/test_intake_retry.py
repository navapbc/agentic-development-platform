from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.orchestration.intake_cursor import CursorRelease
from services.orchestration.intake_retry import (
    IntakeRetryResult,
    PostgresIntakeRetryStore,
    WorkflowLiveness,
    retry_intake,
    temporal_workflow_liveness,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support import hermetic_settings


class _FakeStore:
    def __init__(self, *, workflow_ids: list[str], dedup: int) -> None:
        self._workflow_ids = workflow_ids
        self._dedup = dedup
        self.cleared_repo: str | None = "unset"
        self.marked = False
        self.gate_cleared = False

    async def workflow_ids(self, issue_key: str) -> list[str]:
        return self._workflow_ids

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int:
        self.cleared_repo = repo
        return self._dedup

    async def mark_non_terminal_failed(self, issue_key: str) -> int:
        self.marked = True
        return 1

    async def clear_sequence_gate(self, issue_key: str) -> int:
        self.gate_cleared = True
        return 1


def _active(*running: str):
    async def _check(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.RUNNING if workflow_id in running else WorkflowLiveness.ENDED

    return _check


class _FakeCursor:
    def __init__(self, held: int = 1, warning: str = "") -> None:
        self._held = held
        self._warning = warning
        self.released: list[str] = []

    async def release(self, ticket_key: str) -> CursorRelease:
        self.released.append(ticket_key)
        return CursorRelease(self._held, self._warning)


def _undetermined():
    async def _check(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.UNKNOWN

    return _check


@pytest.mark.asyncio
async def test_retry_clears_dedup_and_marks_stale_execution_failed():
    store = _FakeStore(workflow_ids=["wf-1"], dedup=1)

    result = await retry_intake("ADSER-20", store=store, liveness=_active())

    assert isinstance(result, IntakeRetryResult)
    assert result.retriggered
    assert result.released == 1
    assert result.superseded == 1
    assert store.marked is True
    assert store.gate_cleared is True
    assert "cleared for retrigger" in result.message


@pytest.mark.asyncio
async def test_retry_does_not_reset_the_sequence_gate_when_nothing_is_released():
    store = _FakeStore(workflow_ids=[], dedup=0)

    await retry_intake("ADSER-99", store=store, liveness=_active())

    assert store.gate_cleared is False


@pytest.mark.asyncio
async def test_retry_refuses_when_a_workflow_is_still_running():
    store = _FakeStore(workflow_ids=["wf-live"], dedup=1)

    result = await retry_intake("ADSER-21", store=store, liveness=_active("wf-live"))

    assert not result.retriggered
    assert result.blocked_by == ("wf-live",)
    assert result.released == 0
    assert store.cleared_repo == "unset"
    assert store.marked is False
    assert store.gate_cleared is False
    assert "still has a running workflow" in result.message


@pytest.mark.asyncio
async def test_retry_with_no_intake_record_reports_nothing_to_do():
    store = _FakeStore(workflow_ids=[], dedup=0)

    result = await retry_intake("ADSER-99", store=store, liveness=_active())

    assert not result.retriggered
    assert result.released == 0
    assert store.marked is False
    assert "nothing to retrigger" in result.message


@pytest.mark.asyncio
async def test_retry_passes_repo_scope_to_the_store():
    store = _FakeStore(workflow_ids=[], dedup=1)

    await retry_intake("ADSER-22", store=store, liveness=_active(), repo="agentic-demo-service")

    assert store.cleared_repo == "agentic-demo-service"


@pytest.mark.asyncio
async def test_unreachable_temporal_refuses_without_claiming_a_workflow_runs():
    store = _FakeStore(workflow_ids=["wf-unreachable"], dedup=1)

    result = await retry_intake("ADSER-23", store=store, liveness=_undetermined())

    assert not result.retriggered
    assert result.blocked_by == ()
    assert result.undetermined == ("wf-unreachable",)
    assert result.released == 0
    assert store.marked is False
    assert store.gate_cleared is False
    assert "still has a running workflow" not in result.message
    assert "could not determine" in result.message


@pytest.mark.asyncio
async def test_a_confirmed_running_workflow_outranks_an_undetermined_one():
    store = _FakeStore(workflow_ids=["wf-live", "wf-unreachable"], dedup=1)

    async def _mixed(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.RUNNING if workflow_id == "wf-live" else WorkflowLiveness.UNKNOWN

    result = await retry_intake("ADSER-24", store=store, liveness=_mixed)

    assert result.blocked_by == ("wf-live",)
    assert result.undetermined == ("wf-unreachable",)
    assert "still has a running workflow" in result.message


@pytest.mark.asyncio
async def test_temporal_probe_reports_unknown_when_the_connection_fails(monkeypatch):
    from temporalio.client import Client

    async def _boom(*args, **kwargs):
        raise OSError("connect call failed")

    monkeypatch.setattr(Client, "connect", _boom)
    settings = hermetic_settings(temporal_host="temporal:7233", temporal_namespace="default")

    probe = temporal_workflow_liveness(settings)

    assert await probe("wf-x") is WorkflowLiveness.UNKNOWN


@pytest.mark.asyncio
async def test_retry_releases_the_poll_cursor_alongside_the_ledger():
    store = _FakeStore(workflow_ids=[], dedup=1)
    cursor = _FakeCursor()

    result = await retry_intake("ADEV-30", store=store, liveness=_active(), cursor=cursor)

    assert cursor.released == ["ADEV-30"]
    assert result.cursor_released == 1
    assert result.retriggered
    assert "released the poll cursor" in result.message


@pytest.mark.asyncio
async def test_releasing_only_the_poll_cursor_still_fails_the_stale_executions():
    store = _FakeStore(workflow_ids=[], dedup=0)
    cursor = _FakeCursor()

    result = await retry_intake("ADEV-31", store=store, liveness=_active(), cursor=cursor)

    assert result.released == 0
    assert result.retriggered
    assert store.gate_cleared is True
    # A surviving non-terminal row makes the next pass call the ticket a duplicate and consume
    # the release, so the cursor-only path has to clear it too.
    assert store.marked is True
    assert result.superseded == 1
    assert "nothing to retrigger" not in result.message


@pytest.mark.asyncio
async def test_a_cursor_that_held_nothing_leaves_the_retry_with_nothing_to_do():
    store = _FakeStore(workflow_ids=[], dedup=0)

    result = await retry_intake(
        "ADEV-32", store=store, liveness=_active(), cursor=_FakeCursor(held=0)
    )

    assert not result.retriggered
    assert store.gate_cleared is False
    assert "nothing to retrigger" in result.message


@pytest.mark.asyncio
async def test_a_running_workflow_leaves_the_poll_cursor_alone():
    store = _FakeStore(workflow_ids=["wf-live"], dedup=1)
    cursor = _FakeCursor()

    result = await retry_intake("ADEV-33", store=store, liveness=_active("wf-live"), cursor=cursor)

    assert cursor.released == []
    assert result.cursor_released == 0
    assert not result.retriggered


@pytest.mark.asyncio
async def test_a_cursor_warning_is_reported_without_withholding_the_retrigger():
    store = _FakeStore(workflow_ids=[], dedup=0)
    cursor = _FakeCursor(warning="the rebar store does not list ADEV-34 as ready")

    result = await retry_intake("ADEV-34", store=store, liveness=_active(), cursor=cursor)

    assert result.retriggered
    assert cursor.released == ["ADEV-34"]
    assert "cleared for retrigger" in result.message
    assert "does not list ADEV-34 as ready" in result.message


@pytest.mark.asyncio
async def test_a_warning_from_a_cursor_that_held_nothing_still_reaches_the_operator():
    store = _FakeStore(workflow_ids=[], dedup=0)
    cursor = _FakeCursor(held=0, warning="no rebar poll cursor exists at /srv/x")

    result = await retry_intake("ADEV-35", store=store, liveness=_active(), cursor=cursor)

    assert not result.retriggered
    assert "nothing to retrigger" in result.message
    assert "no rebar poll cursor exists at /srv/x" in result.message


def _like_pattern(pattern: str) -> re.Pattern[str]:
    """SQL LIKE semantics, where % spans any run of characters and _ spans exactly one."""
    return re.compile(
        "".join({"%": ".*", "_": "."}.get(character, re.escape(character)) for character in pattern)
    )


def _flatten(values) -> set[str]:
    flat: set[str] = set()
    for value in values:
        flat.update(value if isinstance(value, list) else [value])
    return flat


@dataclass
class _LedgerRow:
    side_effect_key: str
    task_id: str
    operation: str
    target: str


class _FakeLedgerResult:
    def __init__(self, rows: list[Any], *, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def all(self):
        return self._rows


class _FakeLedgerEngine:
    def __init__(self, rows: list[_LedgerRow]) -> None:
        self.rows = {row.side_effect_key: row for row in rows}

    def begin(self):
        return _FakeLedgerConnection(self)


class _FakeLedgerConnection:
    def __init__(self, engine: _FakeLedgerEngine) -> None:
        self._engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params
        if statement.is_select:
            return _FakeLedgerResult(
                [
                    SimpleNamespace(side_effect_key=row.side_effect_key, target=row.target)
                    for row in self._scoped(params)
                ]
            )
        if statement.is_delete:
            doomed = (
                [key for key in self._engine.rows if key in _flatten(params.values())]
                if "side_effect_key IN " in sql
                else [row.side_effect_key for row in self._scoped(params, sql)]
            )
            for key in doomed:
                del self._engine.rows[key]
            return _FakeLedgerResult([], rowcount=len(doomed))
        raise AssertionError(f"unexpected statement: {statement}")

    def _scoped(self, params: dict[str, Any], sql: str = "") -> list[_LedgerRow]:
        rows = [
            row
            for row in self._engine.rows.values()
            if row.task_id == params["task_id_1"] and row.operation == params["operation_1"]
        ]
        if " LIKE " in sql:
            pattern = _like_pattern(str(params["target_1"]))
            rows = [row for row in rows if pattern.fullmatch(row.target)]
        return rows


def _intake_row(key: str, task_id: str, repo: str) -> _LedgerRow:
    return _LedgerRow(
        side_effect_key=key, task_id=task_id, operation="intake_start", target=f"{task_id}:{repo}"
    )


async def test_clearing_intake_dedup_treats_underscores_in_the_repo_literally():
    engine = _FakeLedgerEngine(
        [
            _intake_row("k-target", "ADEV-1", "my_repo"),
            _intake_row("k-bystander", "ADEV-1", "myXrepo"),
        ]
    )
    store = PostgresIntakeRetryStore(cast(AsyncEngine, engine))

    cleared = await store.clear_intake_dedup("ADEV-1", "my_repo")

    assert cleared == 1
    assert set(engine.rows) == {"k-bystander"}


async def test_clearing_intake_dedup_without_a_repo_clears_every_intake_row():
    engine = _FakeLedgerEngine(
        [
            _intake_row("k-a", "ADEV-1", "repo-a"),
            _intake_row("k-b", "ADEV-1", "repo-b"),
        ]
    )
    store = PostgresIntakeRetryStore(cast(AsyncEngine, engine))

    cleared = await store.clear_intake_dedup("ADEV-1", None)

    assert cleared == 2
    assert set(engine.rows) == set()


async def test_clearing_intake_dedup_without_a_repo_spares_other_tickets():
    engine = _FakeLedgerEngine(
        [
            _intake_row("k-a", "ADEV-1", "repo-a"),
            _intake_row("k-other", "ADEV-2", "repo-a"),
        ]
    )
    store = PostgresIntakeRetryStore(cast(AsyncEngine, engine))

    cleared = await store.clear_intake_dedup("ADEV-1", None)

    assert cleared == 1
    assert set(engine.rows) == {"k-other"}


async def test_clearing_intake_dedup_for_an_unmatched_repo_deletes_nothing():
    engine = _FakeLedgerEngine([_intake_row("k-a", "ADEV-1", "repo-a")])
    store = PostgresIntakeRetryStore(cast(AsyncEngine, engine))

    cleared = await store.clear_intake_dedup("ADEV-1", "repo-b")

    assert cleared == 0
    assert set(engine.rows) == {"k-a"}


@pytest.mark.asyncio
async def test_the_retry_command_exits_non_zero_when_nothing_was_retriggered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.intake import _retry
    from services.orchestration import intake_retry as module

    async def _run(ticket_key: str, *, repo: str | None = None) -> IntakeRetryResult:
        return IntakeRetryResult(ticket_key, released=0, superseded=0, blocked_by=())

    monkeypatch.setattr(module, "run_intake_retry", _run)

    assert await _retry("rebar", "ADEV-50", None) == 1
