from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from services.integration.jira.credentials import StatusFlow
from services.integration.jira.render import heading, link, paragraph, text
from services.integration.tickets import REBAR, TicketBackend
from services.integration.tickets.rebar_backend import (
    RebarTicketBackend,
    render_blocks_to_text,
)


class _FakeRebar(types.ModuleType):
    class ConcurrencyError(Exception):
        pass

    def __init__(self) -> None:
        super().__init__("rebar")
        self.tickets: dict[str, dict[str, Any]] = {}
        self.transitions: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self._counter = 0
        self.raise_concurrency_once = False

    def show_ticket(self, ticket_id: str, *, repo_root: str, include_inbound: bool = False):
        return dict(self.tickets[ticket_id])

    def list_tickets(self, *, repo_root: str, **kwargs: Any):
        return [dict(t) for t in self.tickets.values()]

    def transition(
        self,
        ticket_id: str,
        current_status: str,
        target_status: str,
        *,
        force: bool = False,
        reason: str = "",
        repo_root: str,
        **kwargs: Any,
    ):
        if self.raise_concurrency_once:
            self.raise_concurrency_once = False
            self.tickets[ticket_id]["status"] = target_status
            raise self.ConcurrencyError("stale")
        self.transitions.append((ticket_id, current_status, target_status))
        self.tickets[ticket_id]["status"] = target_status
        return {"ticket_id": ticket_id, "from": current_status, "to": target_status}

    def comment(self, ticket_id: str, body: str, *, repo_root: str, **kwargs: Any):
        self.comments.append((ticket_id, body))
        self.tickets[ticket_id].setdefault("comments", []).append({"body": body})

    def create_ticket(
        self,
        ticket_type: str,
        title: str,
        *,
        description: str | None = None,
        priority: int | None = None,
        repo_root: str,
        **kwargs: Any,
    ) -> str:
        self._counter += 1
        ticket_id = f"tk-{self._counter}"
        self.tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "ticket_type": ticket_type,
            "title": title,
            "description": description or "",
            "priority": priority,
            "status": "open",
            "comments": [],
        }
        return ticket_id


@pytest.fixture
def fake_rebar(monkeypatch: pytest.MonkeyPatch) -> _FakeRebar:
    module = _FakeRebar()
    monkeypatch.setitem(sys.modules, "rebar", module)
    return module


def _backend() -> RebarTicketBackend:
    return RebarTicketBackend(
        repo_root="/tmp/store",
        status_flow=StatusFlow(
            work_started="in_progress", ready_for_review="in_progress", terminal="closed"
        ),
    )


def _seed(fake: _FakeRebar, ticket_id: str = "ADSER-1", status: str = "open") -> None:
    fake.tickets[ticket_id] = {
        "ticket_id": ticket_id,
        "ticket_type": "story",
        "title": "Add pagination",
        "description": "desc",
        "status": status,
        "comments": [],
    }


def test_backend_conforms_to_the_ticket_backend_protocol() -> None:
    assert isinstance(_backend(), TicketBackend)


def test_backend_source_is_rebar() -> None:
    assert _backend().source == REBAR


def test_read_ticket_and_status(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar, status="in_progress")
    backend = _backend()
    assert backend.get_issue("ADSER-1")["title"] == "Add pagination"
    assert backend.get_status("ADSER-1") == "in_progress"


def test_transition_maps_stage_to_configured_status(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar, status="open")
    backend = _backend()
    backend.transition("ADSER-1", backend.status_flow.work_started)
    assert fake_rebar.transitions == [("ADSER-1", "open", "in_progress")]
    assert fake_rebar.tickets["ADSER-1"]["status"] == "in_progress"


def test_transition_is_idempotent_when_already_at_target(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar, status="in_progress")
    backend = _backend()
    backend.transition("ADSER-1", "in_progress")
    assert fake_rebar.transitions == []


def test_transition_swallows_concurrency_when_outcome_matches(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar, status="open")
    fake_rebar.raise_concurrency_once = True
    backend = _backend()
    backend.transition("ADSER-1", "in_progress")
    assert fake_rebar.tickets["ADSER-1"]["status"] == "in_progress"


def test_linkback_posts_a_rebar_comment(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar)
    backend = _backend()
    document = [
        heading("Draft pull request opened"),
        paragraph(link("PR 7", "https://git/pr/7"), text(" on repo")),
    ]
    backend.add_comment("ADSER-1", backend.render_comment(document))
    assert fake_rebar.comments[0][0] == "ADSER-1"
    body = fake_rebar.comments[0][1]
    assert "Draft pull request opened" in body
    assert "https://git/pr/7" in body


def test_render_comment_is_plain_text() -> None:
    document = [
        heading("Title"),
        paragraph(link("PR 7", "https://git/pr/7")),
    ]
    rendered = render_blocks_to_text(document)
    assert rendered == "Title\nPR 7 (https://git/pr/7)"
    assert "{" not in rendered


def test_create_issue_maps_jira_shaped_fields(fake_rebar: _FakeRebar) -> None:
    backend = _backend()
    created = backend.create_issue(
        {"issue_type": "story", "summary": "New work", "description": "body", "priority": 2}
    )
    assert created["title"] == "New work"
    assert created["ticket_type"] == "story"
    assert created["priority"] == 2


def test_unset_lifecycle_stage_is_never_invented(fake_rebar: _FakeRebar) -> None:
    _seed(fake_rebar, status="open")
    backend = RebarTicketBackend(
        repo_root="/tmp/store",
        status_flow=StatusFlow(work_started="in_progress"),
    )
    assert backend.status_flow.ready_for_review == ""
    assert backend.status_flow.terminal == ""
