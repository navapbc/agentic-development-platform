from __future__ import annotations

import pytest
from scripts.rebar_poll import (
    FIELD_CHANGE,
    NEW,
    STATUS_CHANGE,
    AccessDecision,
    PollCursor,
    PollEvent,
    PollFlags,
    redact,
    run_poll,
)


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
        return [dict(t) for t in self._tickets]


class _Watermark:
    def __init__(self, cursor: PollCursor | None = None) -> None:
        self.cursor = cursor or PollCursor()
        self.saves = 0

    def load(self) -> PollCursor:
        return PollCursor(
            branch_commit=self.cursor.branch_commit,
            revisions=dict(self.cursor.revisions),
            statuses=dict(self.cursor.statuses),
        )

    def save(self, cursor: PollCursor) -> None:
        self.cursor = cursor
        self.saves += 1


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, event: dict) -> None:
        self.records.append(event)

    def events(self) -> list[str]:
        return [r["event"] for r in self.records]


class _Access:
    def __init__(self, decision: AccessDecision) -> None:
        self._decision = decision

    def authorize(self) -> AccessDecision:
        return self._decision


def _authorized() -> _Access:
    return _Access(AccessDecision(authorized=True, identity="reader@x", role="tickets-reader"))


def _ticket(ticket_id: str, *, status: str = "open", title: str = "T", secret: str = "s") -> dict:
    return {"ticket_id": ticket_id, "status": status, "title": title, "secret_field": secret}


_ALLOWED = ("ticket_id", "status", "title")


@pytest.mark.asyncio
async def test_new_ticket_reported_once_then_not_replayed() -> None:
    reader = _Reader([_ticket("A")])
    watermark = _Watermark()
    audit = _Audit()

    handled: list[str] = []

    async def intake(event: PollEvent) -> bool:
        handled.append(event.ticket_id)
        return True

    first = await run_poll(
        reader=reader,
        watermark=watermark,
        audit=audit,
        access=_authorized(),
        allowed_fields=_ALLOWED,
        intake=intake,
    )
    assert [e.kind for e in first.events] == [NEW]
    assert first.advanced is True
    assert handled == ["A"]

    second = await run_poll(
        reader=reader,
        watermark=watermark,
        audit=audit,
        access=_authorized(),
        allowed_fields=_ALLOWED,
        intake=intake,
    )
    assert second.events == ()
    assert handled == ["A"]


@pytest.mark.asyncio
async def test_status_change_is_classified() -> None:
    reader = _Reader([_ticket("A", status="open")])
    watermark = _Watermark()
    audit = _Audit()

    await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    reader.set([_ticket("A", status="in_progress")], head="commit-2")
    report = await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    assert [e.kind for e in report.events] == [STATUS_CHANGE]
    assert report.events[0].previous_status == "open"


@pytest.mark.asyncio
async def test_field_change_is_classified() -> None:
    reader = _Reader([_ticket("A", title="T1")])
    watermark = _Watermark()
    audit = _Audit()
    await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    reader.set([_ticket("A", title="T2")], head="commit-2")
    report = await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    assert [e.kind for e in report.events] == [FIELD_CHANGE]


@pytest.mark.asyncio
async def test_unauthorized_caller_is_rejected_and_audited() -> None:
    reader = _Reader([_ticket("A")])
    watermark = _Watermark()
    audit = _Audit()
    access = _Access(AccessDecision(authorized=False, identity="x", role="none"))

    report = await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=access,
        allowed_fields=_ALLOWED,
    )
    assert report.authorized is False
    assert watermark.saves == 0
    assert "rebar_poll_invoked" in audit.events()
    assert "rebar_poll_denied" in audit.events()


@pytest.mark.asyncio
async def test_non_allowlisted_fields_are_redacted() -> None:
    reader = _Reader([_ticket("A", secret="top-secret")])
    report = await run_poll(
        reader=reader, watermark=_Watermark(), audit=_Audit(), access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    assert "secret_field" not in report.events[0].ticket
    assert report.events[0].ticket["ticket_id"] == "A"


@pytest.mark.asyncio
async def test_noop_invocation_is_audited() -> None:
    reader = _Reader([])
    audit = _Audit()
    report = await run_poll(
        reader=reader, watermark=_Watermark(), audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    assert report.events == ()
    assert "rebar_poll_invoked" in audit.events()
    assert "rebar_poll_noop" in audit.events()


@pytest.mark.asyncio
async def test_events_carry_revision_and_branch_commit_before_side_effects() -> None:
    reader = _Reader([_ticket("A")], head="deadbeef")
    audit = _Audit()
    seen: list[tuple[str, str]] = []

    async def intake(event: PollEvent) -> bool:
        seen.append((event.revision, event.branch_commit))
        return True

    report = await run_poll(
        reader=reader, watermark=_Watermark(), audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED, intake=intake,
    )
    assert report.events[0].branch_commit == "deadbeef"
    assert report.events[0].revision != ""
    change_audits = [r for r in audit.records if r["event"] == f"rebar_poll_{NEW}"]
    assert change_audits and change_audits[0]["branch_commit"] == "deadbeef"
    assert seen == [(report.events[0].revision, "deadbeef")]


@pytest.mark.asyncio
async def test_failed_intake_does_not_advance_and_replays() -> None:
    reader = _Reader([_ticket("A")])
    watermark = _Watermark()
    audit = _Audit()
    attempts: list[str] = []

    async def failing(event: PollEvent) -> bool:
        attempts.append(event.ticket_id)
        return False

    first = await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED, intake=failing,
    )
    assert first.advanced is False
    assert watermark.cursor.branch_commit == ""

    async def ok(event: PollEvent) -> bool:
        attempts.append(event.ticket_id)
        return True

    second = await run_poll(
        reader=reader, watermark=watermark, audit=audit, access=_authorized(),
        allowed_fields=_ALLOWED, intake=ok,
    )
    assert [e.kind for e in second.events] == [NEW]
    assert attempts == ["A", "A"]


@pytest.mark.asyncio
async def test_flags_select_categories() -> None:
    reader = _Reader([_ticket("A", status="open")])
    watermark = _Watermark()
    await run_poll(
        reader=reader, watermark=watermark, audit=_Audit(), access=_authorized(),
        allowed_fields=_ALLOWED,
    )
    reader.set([_ticket("A", status="in_progress")], head="commit-2")
    report = await run_poll(
        reader=reader, watermark=watermark, audit=_Audit(), access=_authorized(),
        allowed_fields=_ALLOWED, flags=PollFlags(
            report_new=False, report_field_changes=False, report_status_changes=False
        ),
    )
    assert report.events == ()


def test_redact_keeps_only_allowlisted_fields() -> None:
    result = redact({"a": 1, "b": 2, "c": 3}, ("a", "c"))
    assert result == {"a": 1, "c": 3}
