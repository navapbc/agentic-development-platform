"""How the rebar intake path moves the poll watermark past a ticket it will not admit."""

from __future__ import annotations

from typing import Any

import pytest
from scripts.rebar_poll import AccessDecision, PollCursor
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.intake_reconciler import IntakeContext
from services.orchestration.rebar_intake import reconcile_rebar_intake
from tests.support import ModelPathStub, capture_alerts, hermetic_settings, model_path_gate

_ALLOWED = ("ticket_id", "status", "title")


class _Unused:
    """Stands in for an intake collaborator that a pass admitting no ticket never calls."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"an intake collaborator was used: {name}")


_UNUSED: Any = _Unused()


class _Reader:
    def __init__(self, tickets: list[dict], head: str = "commit-1") -> None:
        self._tickets = tickets
        self._head = head

    def set(self, tickets: list[dict], head: str) -> None:
        self._tickets = tickets
        self._head = head

    def branch_head(self) -> str:
        return self._head

    def list_reduced(self) -> list[dict]:
        return [dict(ticket) for ticket in self._tickets]


class _Watermark:
    def __init__(self) -> None:
        self.cursor = PollCursor()

    def load(self) -> PollCursor:
        return PollCursor(
            branch_commit=self.cursor.branch_commit,
            revisions=dict(self.cursor.revisions),
            statuses=dict(self.cursor.statuses),
        )

    def save(self, cursor: PollCursor) -> None:
        self.cursor = cursor


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, event: dict) -> None:
        self.records.append(event)

    def events(self) -> list[str]:
        return [record["event"] for record in self.records]


class _Access:
    def authorize(self) -> AccessDecision:
        return AccessDecision(authorized=True, identity="reader@x", role="tickets-reader")


def _ticket(ticket_id: str, *, status: str) -> dict:
    return {"ticket_id": ticket_id, "status": status, "title": "T"}


async def _reconcile(reader: _Reader, watermark: _Watermark, audit: _Audit, ready: set[str]):
    def _unreachable(ticket_id: str) -> dict:
        raise AssertionError(f"a ticket the poll will not admit was read: {ticket_id}")

    return await reconcile_rebar_intake(
        reader=reader,
        watermark=watermark,
        audit=audit,
        access=_Access(),
        allowed_fields=_ALLOWED,
        ctx=IntakeContext(
            starter=_UNUSED,
            alerter=_UNUSED,
            ledger=_UNUSED,
            execution_seeder=_UNUSED,
            snapshot_store=None,
            github_allow=frozenset(),
            field_id="",
            settings=hermetic_settings(),
        ),
        show_ticket=_unreachable,
        is_ready=lambda ticket_id: ticket_id in ready,
        project="ADEV",
        default_repo="agentic-demo-service",
    )


@pytest.mark.asyncio
async def test_a_ticket_that_is_not_admissible_is_counted_as_blocked() -> None:
    reader = _Reader([_ticket("A", status="closed")])

    report = await _reconcile(reader, _Watermark(), _Audit(), ready=set())

    assert report.blocked == 1
    assert report.started == 0


@pytest.mark.asyncio
async def test_the_watermark_records_a_ticket_the_poll_will_not_admit() -> None:
    reader = _Reader([_ticket("A", status="closed")])
    watermark = _Watermark()

    await _reconcile(reader, watermark, _Audit(), ready=set())

    assert watermark.cursor.statuses.get("A") == "closed"
    assert watermark.cursor.branch_commit == "commit-1"


@pytest.mark.asyncio
async def test_an_unadmitted_ticket_is_not_replayed_on_the_next_poll() -> None:
    reader = _Reader([_ticket("A", status="closed")])
    watermark = _Watermark()

    await _reconcile(reader, watermark, _Audit(), ready=set())
    audit = _Audit()
    report = await _reconcile(reader, watermark, audit, ready=set())

    assert report.blocked == 0
    assert "rebar_poll_noop" in audit.events()
    assert not [event for event in audit.events() if event.endswith(("_new", "_status_change"))]


@pytest.mark.asyncio
async def test_a_ticket_that_becomes_admissible_later_is_still_seen() -> None:
    reader = _Reader([_ticket("A", status="closed")])
    watermark = _Watermark()

    await _reconcile(reader, watermark, _Audit(), ready=set())
    reader.set([_ticket("A", status="open")], head="commit-2")
    audit = _Audit()
    report = await _reconcile(reader, watermark, audit, ready=set())

    assert report.blocked == 1
    assert "rebar_poll_status_change" in audit.events()


class _Starter:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start(self, start) -> str:
        self.started.append(start.issue_key)
        return f"wf-{start.issue_key}"


class _Seeder:
    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        return None

    async def abandon(self, *, execution_id: str, reason: str) -> None:
        return None


class _SnapshotStore:
    async def persist(self, candidate) -> TaskSnapshot:
        return TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, 1),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=1,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )


@pytest.mark.asyncio
async def test_a_ticket_deferred_on_an_unhealthy_model_path_is_read_again(monkeypatch) -> None:
    capture_alerts(monkeypatch)
    reader = _Reader([_ticket("A", status="open")])
    watermark = _Watermark()
    starter = _Starter()
    path = ModelPathStub("credentials")
    ledger = InMemorySideEffectLedger()
    ctx = IntakeContext(
        starter=starter,
        alerter=_UNUSED,
        ledger=ledger,
        execution_seeder=_Seeder(),
        snapshot_store=_SnapshotStore(),
        github_allow=frozenset({"agentic-demo-service"}),
        field_id="",
        settings=hermetic_settings(),
        model_path=model_path_gate(path, ledger=ledger),
    )

    async def _pass():
        return await reconcile_rebar_intake(
            reader=reader,
            watermark=watermark,
            audit=_Audit(),
            access=_Access(),
            allowed_fields=_ALLOWED,
            ctx=ctx,
            show_ticket=lambda ticket_id: _ticket(ticket_id, status="open"),
            is_ready=lambda ticket_id: True,
            project="ADEV",
            default_repo="agentic-demo-service",
        )

    deferred = await _pass()
    path.hop = ""
    path.next_pass()
    started = await _pass()

    assert (deferred.deferred, deferred.started) == (1, 0)
    assert (started.deferred, started.started) == (0, 1)
    assert starter.started == ["A"]
