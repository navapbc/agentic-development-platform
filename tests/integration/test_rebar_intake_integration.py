from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts.rebar_poll import (
    NEW,
    AccessDecision,
    PollCursor,
    PollEvent,
    run_poll,
)
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeStart,
    candidate_from_rebar,
    route_candidate,
)
from tests.support import hermetic_settings

pytest.importorskip("rebar")

_ALLOW = frozenset({"agentic-demo-service"})
_ALLOWED_FIELDS = ("ticket_id", "title", "status", "description", "repos", "tags")


class _Reader:
    def __init__(self, repo_root: Path) -> None:
        self._repo_root = str(repo_root)

    def branch_head(self) -> str:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "tickets"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def list_reduced(self) -> list[dict]:
        import rebar

        return [dict(t) for t in rebar.list_tickets(repo_root=self._repo_root)]


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


class _Access:
    def authorize(self) -> AccessDecision:
        return AccessDecision(authorized=True, identity="reader@x", role="tickets-reader")


class _Starter:
    def __init__(self) -> None:
        self.starts: list[IntakeStart] = []

    async def start(self, start: IntakeStart) -> str:
        self.starts.append(start)
        return f"wf-{start.issue_key}-{start.repo}"


class _Alerter:
    async def unroutable(self, *, issue_key: str, project: str, reason: str, remedy: str) -> None:
        return None


class _Seeder:
    def __init__(self) -> None:
        self.seeded: list[str] = []

    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        self.seeded.append(task_id)


@dataclass
class _SnapshotStore:
    def __init__(self) -> None:
        self.persisted: list[str] = []

    async def persist(self, candidate) -> TaskSnapshot:
        self.persisted.append(candidate.task_id)
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


@pytest.fixture
def seeded_store(tmp_path: Path) -> Path:
    from tests.support import seed_rebar_store

    root = tmp_path / "rebar-tickets"
    seed_rebar_store(root)
    return root


@pytest.mark.asyncio
async def test_poll_drives_rebar_intake_through_candidate_model(seeded_store: Path) -> None:
    reader = _Reader(seeded_store)
    starter = _Starter()
    ledger = InMemorySideEffectLedger()
    seeder = _Seeder()
    snapshot_store = _SnapshotStore()
    ctx = IntakeContext(
        starter=starter,
        alerter=_Alerter(),
        ledger=ledger,
        execution_seeder=seeder,
        snapshot_store=snapshot_store,
        github_allow=_ALLOW,
        field_id="",
        settings=hermetic_settings(),
    )

    async def intake(event: PollEvent) -> bool:
        import rebar

        ticket = rebar.show_ticket(event.ticket_id, repo_root=str(seeded_store))
        candidate = candidate_from_rebar(
            ticket, project="ADSER", default_repo="agentic-demo-service"
        )
        report = _Report()
        await route_candidate(
            candidate, project="ADSER", component_map=None, ctx=ctx, report=report
        )
        return True

    result = await run_poll(
        reader=reader,
        watermark=_Watermark(),
        audit=_Audit(),
        access=_Access(),
        allowed_fields=_ALLOWED_FIELDS,
        intake=intake,
    )

    assert all(e.kind == NEW for e in result.events)
    assert len(result.events) == 2
    started_ids = {s.issue_key for s in starter.starts}
    assert len(started_ids) == 2
    assert all(s.repo == "agentic-demo-service" for s in starter.starts)
    assert all(s.stored_issue["project"] == "ADSER" for s in starter.starts)


class _Report:
    def __init__(self) -> None:
        self.started: list = []
        self.held: list = []
