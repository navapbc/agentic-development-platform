from __future__ import annotations

from pathlib import Path

import pytest
from services.integration.jira.credentials import StatusFlow
from services.integration.jira.render import heading, paragraph, text
from services.integration.tickets.rebar_backend import RebarTicketBackend

pytest.importorskip("rebar")


@pytest.fixture
def seeded_store(tmp_path: Path) -> Path:
    from tests.support import seed_rebar_store

    root = tmp_path / "rebar-tickets"
    seed_rebar_store(root)
    return root


def _backend(root: Path) -> RebarTicketBackend:
    return RebarTicketBackend(
        repo_root=str(root),
        status_flow=StatusFlow(
            work_started="in_progress", ready_for_review="in_progress", terminal="closed"
        ),
    )


def _first_ticket_id(root: Path) -> str:
    import rebar

    tickets = rebar.list_tickets(repo_root=str(root))
    return str(tickets[0]["ticket_id"])


def test_reads_seeded_tickets(seeded_store: Path) -> None:
    backend = _backend(seeded_store)
    tickets = backend.search_issues("")
    assert len(tickets) == 2
    titles = {t["title"] for t in tickets}
    assert "Add pagination to the task list endpoint" in titles
    assert all(t["status"] == "open" for t in tickets)


def test_transition_through_backend_updates_store(seeded_store: Path) -> None:
    backend = _backend(seeded_store)
    ticket_id = _first_ticket_id(seeded_store)

    backend.transition(ticket_id, backend.status_flow.work_started)

    assert backend.get_status(ticket_id) == "in_progress"


def test_comment_through_backend_reads_back(seeded_store: Path) -> None:
    backend = _backend(seeded_store)
    ticket_id = _first_ticket_id(seeded_store)
    document = [heading("Draft pull request opened"), paragraph(text("linkback body"))]

    backend.add_comment(ticket_id, backend.render_comment(document))

    bodies = backend.list_comment_bodies(ticket_id)
    assert any("linkback body" in body for body in bodies)


def test_create_issue_persists(seeded_store: Path) -> None:
    backend = _backend(seeded_store)
    created = backend.create_issue(
        {"issue_type": "story", "summary": "New enhancement", "description": "body", "priority": 2}
    )
    assert created["title"] == "New enhancement"
    assert backend.get_status(created["ticket_id"]) == "open"
