"""Idempotency-primitive tests: deterministic keys and replay-is-a-no-op semantics."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from services.coordination.idempotency import (
    InMemorySideEffectLedger,
    PostgresSideEffectLedger,
    SideEffectRef,
    side_effect_key,
)
from sqlalchemy.dialects import postgresql


@dataclass
class _SideEffectRow:
    side_effect_key: str
    task_id: str
    execution_id: str
    operation: str
    target: str
    status: str
    result: Any = None
    updated_at: int = 0


class _FakeResult:
    def __init__(self, rows: list[Any], *, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeEngine:
    def __init__(self, rows: list[_SideEffectRow]) -> None:
        self.rows = {row.side_effect_key: row for row in rows}

    def begin(self):
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, engine: _FakeEngine) -> None:
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
            return _FakeResult(self._select(sql, params))
        if statement.is_insert:
            key = str(params["side_effect_key"])
            self._engine.rows.setdefault(
                key,
                _SideEffectRow(
                    side_effect_key=key,
                    task_id=str(params["task_id"]),
                    execution_id=str(params["execution_id"]),
                    operation=str(params["operation"]),
                    target=str(params["target"]),
                    status=str(params["status"]),
                ),
            )
            return _FakeResult([], rowcount=1)
        if statement.is_update:
            row = self._engine.rows.get(str(params["side_effect_key_1"]))
            if row is None:
                return _FakeResult([], rowcount=0)
            row.status = str(params["status"])
            row.result = params["result"]
            return _FakeResult([], rowcount=1)
        raise AssertionError(f"unexpected statement: {statement}")

    def _select(self, sql: str, params: dict[str, Any]) -> list[Any]:
        if "side_effect_key = " in sql:
            row = self._engine.rows.get(str(params["side_effect_key_1"]))
            return [_row_result(row)] if row is not None else []
        rows = [
            row
            for row in self._engine.rows.values()
            if row.task_id == params["task_id_1"]
            and row.execution_id == params["execution_id_1"]
            and row.operation == params["operation_1"]
        ]
        if "ORDER BY" in sql:
            assert "updated_at DESC" in sql
            rows.sort(key=lambda row: row.updated_at, reverse=True)
        return [_row_result(row) for row in rows]


def _row_result(row: _SideEffectRow):
    return SimpleNamespace(
        side_effect_key=row.side_effect_key,
        status=row.status,
        result=row.result,
        target=row.target,
    )


def test_key_is_deterministic_and_field_sensitive():
    base = SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")

    assert side_effect_key(base) == side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "Done")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e2", "jira_transition", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_comment", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-2", "e1", "jira_transition", "In Progress")
    )


async def test_replay_returns_recorded_result_without_rerunning_effect():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")
    calls: list[int] = []

    def effect():
        calls.append(1)
        return {"transitioned": True}

    first = await ledger.run_once(ref, effect)
    second = await ledger.run_once(ref, effect)

    assert first.replayed is False
    assert second.replayed is True
    assert len(calls) == 1
    assert second.result == {"transitioned": True}
    assert first.key == second.key


async def test_distinct_refs_each_execute():
    ledger = InMemorySideEffectLedger()
    calls: list[str] = []

    async def make(target: str):
        ref = SideEffectRef("AGENTDEV-1", "e1", "jira_comment", target)

        def effect():
            calls.append(target)
            return {"target": target}

        return await ledger.run_once(ref, effect)

    a = await make("pr_link")
    b = await make("summary")

    assert a.replayed is False
    assert b.replayed is False
    assert calls == ["pr_link", "summary"]


async def test_legacy_digest_based_intake_start_replays_for_the_same_ticket_repo():
    ledger = InMemorySideEffectLedger()
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    calls: list[str] = []

    async def start_legacy():
        calls.append("legacy")
        return {"workflow_id": "wf-legacy"}

    async def start_current():
        calls.append("current")
        return {"workflow_id": "wf-current"}

    first = await ledger.run_once(legacy, start_legacy)
    second = await ledger.run_once(current, start_current)

    assert first.replayed is False
    assert second.replayed is True
    assert calls == ["legacy"]
    assert second.result == {"workflow_id": "wf-legacy"}


async def test_legacy_pending_intake_start_is_completed_under_the_current_key():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="pending",
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)
    calls: list[str] = []

    async def start_current():
        calls.append("current")
        return {"workflow_id": "wf-current"}

    first = await ledger.run_once(current, start_current)
    second = await ledger.run_once(current, start_current)

    assert first.replayed is False
    assert second.replayed is True
    assert calls == ["current"]
    assert engine.rows[side_effect_key(current)].status == "completed"
    assert engine.rows[side_effect_key(current)].result == {"workflow_id": "wf-current"}


async def test_pending_legacy_intake_start_row_makes_the_current_key_reentrant():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="pending",
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)

    outcome = await ledger.reserve(current)

    assert outcome.replayed is False
    assert outcome.reentrant is True
    assert outcome.key == side_effect_key(current)


async def test_pending_legacy_intake_start_row_for_another_repo_is_not_reentrant():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-b")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="pending",
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)

    outcome = await ledger.reserve(current)

    assert outcome.replayed is False
    assert outcome.reentrant is False


async def test_the_most_recently_updated_legacy_intake_start_row_wins():
    stale = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    fresh = SideEffectRef("ADEV-1", "", "intake_start", "digest-2:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(stale),
                task_id=stale.task_id,
                execution_id=stale.execution_id,
                operation=stale.operation,
                target=stale.target,
                status="pending",
                updated_at=1,
            ),
            _SideEffectRow(
                side_effect_key=side_effect_key(fresh),
                task_id=fresh.task_id,
                execution_id=fresh.execution_id,
                operation=fresh.operation,
                target=fresh.target,
                status="completed",
                result={"workflow_id": "wf-fresh"},
                updated_at=2,
            ),
        ]
    )
    ledger = PostgresSideEffectLedger(engine)

    outcome = await ledger.reserve(current)

    assert outcome.replayed is True
    assert outcome.key == side_effect_key(fresh)
    assert outcome.result == {"workflow_id": "wf-fresh"}


async def test_legacy_completed_intake_start_replays_with_the_legacy_key():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="completed",
                result={"workflow_id": "wf-legacy"},
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)
    calls: list[str] = []

    async def start_current():
        calls.append("current")
        return {"workflow_id": "wf-current"}

    outcome = await ledger.run_once(current, start_current)

    assert outcome.replayed is True
    assert outcome.key == side_effect_key(legacy)
    assert outcome.key != side_effect_key(current)
    assert outcome.result == {"workflow_id": "wf-legacy"}
    assert calls == []
    assert side_effect_key(current) not in engine.rows


async def test_legacy_completed_intake_start_for_a_different_repo_does_not_replay():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-b")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="completed",
                result={"workflow_id": "wf-legacy"},
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)
    calls: list[str] = []

    async def start_current():
        calls.append("current")
        return {"workflow_id": "wf-current"}

    outcome = await ledger.run_once(current, start_current)

    assert outcome.replayed is False
    assert outcome.key == side_effect_key(current)
    assert calls == ["current"]
    assert engine.rows[side_effect_key(current)].result == {"workflow_id": "wf-current"}


async def test_legacy_intake_repo_match_treats_underscores_literally():
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repoXa")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo_a")
    engine = _FakeEngine(
        [
            _SideEffectRow(
                side_effect_key=side_effect_key(legacy),
                task_id=legacy.task_id,
                execution_id=legacy.execution_id,
                operation=legacy.operation,
                target=legacy.target,
                status="completed",
                result={"workflow_id": "wf-legacy"},
            )
        ]
    )
    ledger = PostgresSideEffectLedger(engine)
    calls: list[str] = []

    async def start_current():
        calls.append("current")
        return {"workflow_id": "wf-current"}

    outcome = await ledger.run_once(current, start_current)

    assert outcome.replayed is False
    assert calls == ["current"]
    assert engine.rows[side_effect_key(current)].result == {"workflow_id": "wf-current"}


async def test_supports_async_effect_for_evidence_writes():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "evidence_write", "content-hash-1")
    writes: list[str] = []

    async def persist():
        writes.append("content-hash-1")
        return {"artifact_id": "a1"}

    first = await ledger.run_once(ref, persist)
    second = await ledger.run_once(ref, persist)

    assert first.result == {"artifact_id": "a1"}
    assert second.replayed is True
    assert writes == ["content-hash-1"]


async def test_reserving_a_still_pending_ref_again_reports_reentry():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "github.commit", "demo:agent/AGENTDEV-1")

    first = await ledger.reserve(ref)
    second = await ledger.reserve(ref)

    assert (first.replayed, first.reentrant) == (False, False)
    assert (second.replayed, second.reentrant) == (False, True)


async def test_reserving_after_completion_replays_rather_than_reentering():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "github.commit", "demo:agent/AGENTDEV-1")

    await ledger.reserve(ref)
    await ledger.record(ref, {"sha": "abc"})
    outcome = await ledger.reserve(ref)

    assert outcome.replayed is True
    assert outcome.reentrant is False
    assert outcome.result == {"sha": "abc"}


async def test_pending_legacy_intake_start_makes_the_current_key_reentrant():
    ledger = InMemorySideEffectLedger()
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-a")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")

    await ledger.reserve(legacy)
    outcome = await ledger.reserve(current)

    assert outcome.replayed is False
    assert outcome.reentrant is True


async def test_pending_intake_start_for_a_different_repo_is_not_reentrant():
    ledger = InMemorySideEffectLedger()
    legacy = SideEffectRef("ADEV-1", "", "intake_start", "digest-1:repo-b")
    current = SideEffectRef("ADEV-1", "", "intake_start", "ADEV-1:repo-a")

    await ledger.reserve(legacy)
    outcome = await ledger.reserve(current)

    assert outcome.replayed is False
    assert outcome.reentrant is False
