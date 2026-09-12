"""The dependency link graph capability and the link write-back."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.integration.jira.credentials import StatusFlow
from services.integration.tickets import DependencyBackend
from services.integration.tickets.rebar_backend import RebarTicketBackend
from services.integration.tickets.write_contract import (
    LINK,
    WriteBackContext,
    WriteBackError,
    authorize_write_back,
)
from tests.support import seed_rebar_store


def _backend(root: Path) -> RebarTicketBackend:
    return RebarTicketBackend(repo_root=str(root), status_flow=StatusFlow(), project="")


def test_rebar_backend_is_a_dependency_backend(tmp_path):
    seed_rebar_store(tmp_path, tickets=[{"title": "A"}])
    assert isinstance(_backend(tmp_path), DependencyBackend)


def test_link_records_a_dependency(tmp_path):
    ids = seed_rebar_store(tmp_path, tickets=[{"title": "A"}, {"title": "B"}])
    backend = _backend(tmp_path)
    backend.link(ids[0], ids[1], relationship="depends_on")
    assert ids[1] in backend.declared_dependencies(ids[0])


def test_link_is_idempotent(tmp_path):
    ids = seed_rebar_store(tmp_path, tickets=[{"title": "A"}, {"title": "B"}])
    backend = _backend(tmp_path)
    backend.link(ids[0], ids[1], relationship="depends_on")
    backend.link(ids[0], ids[1], relationship="depends_on")
    assert list(backend.declared_dependencies(ids[0])).count(ids[1]) == 1


def test_file_impact_leaves_the_dependency_capability(tmp_path):
    seed_rebar_store(tmp_path, tickets=[{"title": "A"}])
    backend = _backend(tmp_path)
    assert not hasattr(backend, "record_file_impact")
    assert not hasattr(backend, "read_file_impact")
    assert backend.dependency_graph_active() is True


def test_authorize_link_rejects_an_unowned_ticket():
    ctx = WriteBackContext(
        actor="agent", backend="rebar", project="", issue_key="AAA-1", run_id="r1"
    )
    with pytest.raises(WriteBackError):
        authorize_write_back(ctx, scope=LINK, run_owner_issue_key="BBB-2")


def test_authorize_link_accepts_the_owned_ticket():
    ctx = WriteBackContext(
        actor="agent", backend="rebar", project="", issue_key="AAA-1", run_id="r1"
    )
    authorize_write_back(ctx, scope=LINK, run_owner_issue_key="AAA-1")


class _PlainBackend:
    def get_issue(self, issue_key: str) -> dict:
        return {}


class _CapableBackend:
    def __init__(self) -> None:
        self.links: list[tuple[str, str, str]] = []

    def link(self, from_issue: str, to_issue: str, *, relationship: str) -> None:
        self.links.append((from_issue, to_issue, relationship))

    def dependency_graph_active(self) -> bool:
        return True

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return ()

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        return ()


async def test_record_link_activity_skips_a_backend_without_the_capability(monkeypatch):
    from services.orchestration.temporal_activities import pull_request as pr

    monkeypatch.setattr(pr, "_build_ticket_backend", lambda issue_key: _PlainBackend())
    result = await pr.record_dependency_link(
        pr.RecordDependencyLinkRequest(
            dependent_issue_key="AAA-1", blocking_issue_key="AAA-2", execution_id="e1"
        )
    )
    assert result.linked is False


async def test_record_link_activity_links_and_audits(monkeypatch):
    from services.orchestration.temporal_activities import pull_request as pr

    backend = _CapableBackend()
    monkeypatch.setattr(pr, "_build_ticket_backend", lambda issue_key: backend)
    monkeypatch.setattr(
        "services.integration.tickets.resolve_ticket_backend_name", lambda *a, **k: "rebar"
    )
    monkeypatch.setattr(
        "services.integration.tickets.write_contract.load_write_back_scopes",
        lambda *a, **k: (LINK,),
    )
    result = await pr.record_dependency_link(
        pr.RecordDependencyLinkRequest(
            dependent_issue_key="AAA-1", blocking_issue_key="AAA-2", execution_id="e1"
        )
    )
    assert result.linked is True
    assert backend.links == [("AAA-1", "AAA-2", "depends_on")]
