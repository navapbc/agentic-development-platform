"""End-to-end exit proof: a local rebar ticket reaches a draft PR and its store records the run.

Hermetic. Seeds a git-backed rebar store, polls it, routes the candidate through intake, and
runs the bounded pipeline with the rebar ticket backend serving the pre-PR recheck, the
lifecycle transitions, and the linkback, against a bare git remote and a mock GHES. The code
step is a stub, so no LLM endpoint is reached and the test is safe to run in CI. The live
counterpart that drives a model is the env-gated smoke at the bottom.

This closes the seam the other exit proofs leave open: ``test_rebar_intake_integration`` stops
at a stub starter and ``test_bounded_task_end_to_end`` starts from a Jira-canonical snapshot,
so neither proves a locally stored ticket survives claim, recheck, PR linkback, and transition.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from adapters.interfaces.git_host import agent_branch_name
from services.coordination import (
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
)
from services.coordination.orphans import InMemoryOrphanRegistry
from services.integration.github.mediator import GitHubMediator
from services.integration.jira import APPLIED
from services.integration.jira.credentials import StatusFlow
from services.integration.jira.edits import MATERIAL_CHANGE
from services.integration.tickets.rebar_backend import RebarTicketBackend
from services.orchestration.bounded_task_pipeline import (
    DRAFT_PR_OPEN,
    RECHECK_PAUSED,
    PipelineDeps,
    PipelineRequest,
    run_bounded_task,
)
from services.orchestration.branch_sync import GitBranchSynchronizer
from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeStart,
    candidate_from_rebar,
    route_candidate,
)
from services.orchestration.materialization import WorkspaceMaterializer
from tests.integration.test_bounded_task_end_to_end import (
    _fake_ghes,
    _FakeCodeStep,
    _host,
    _InMemorySnapshotStore,
    _remote_head,
)
from tests.integration.test_rebar_intake_integration import (
    _ALLOW,
    _ALLOWED_FIELDS,
    _Access,
    _Alerter,
    _Audit,
    _Reader,
    _Seeder,
    _SnapshotStore,
    _Starter,
    _Watermark,
)
from tests.support import hermetic_settings, seed_rebar_store

pytest.importorskip("rebar")

REPO = "agentic-demo-service"
PROJECT = "ADSER"

# The rebar store has no distinct review state, so the configured flow resolves both
# lifecycle stages onto in_progress. See config/local/tickets.yaml.
FLOW = StatusFlow(work_started="in_progress", ready_for_review="in_progress", terminal="closed")

TICKET = {
    "ticket_type": "task",
    "title": "Add pagination to the task list endpoint",
    "description": "Add limit and offset to GET /tasks.",
    "priority": 2,
    "repos": [REPO],
    "tags": ["enhancement", "api"],
}


@pytest.fixture
def bare_remote(tmp_path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    identity = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    staged = ["add", "."]
    committed = [*identity, "commit", "-q", "-m", "init"]
    pushed = ["push", "-q", "origin", "main"]
    for args in (staged, committed, pushed):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True, text=True)
    return str(remote)


@pytest.fixture
def seeded_store(tmp_path: Path) -> Path:
    root = tmp_path / "rebar-tickets"
    seed_rebar_store(root, [TICKET])
    return root


def _backend(store: Path) -> RebarTicketBackend:
    return RebarTicketBackend(repo_root=str(store), status_flow=FLOW, project=PROJECT)


async def _route_one_ticket(store: Path) -> IntakeStart:
    """Poll the store and route its single ticket, returning the start the workflow receives."""
    import rebar
    from scripts.rebar_poll import NEW, run_poll

    starter = _Starter()
    ctx = IntakeContext(
        starter=starter,
        alerter=_Alerter(),
        ledger=InMemorySideEffectLedger(),
        execution_seeder=_Seeder(),
        snapshot_store=_SnapshotStore(),
        github_allow=_ALLOW,
        field_id="",
        settings=hermetic_settings(),
    )

    async def intake(event) -> bool:
        ticket = rebar.show_ticket(event.ticket_id, repo_root=str(store))
        candidate = candidate_from_rebar(ticket, project=PROJECT, default_repo=REPO)

        class _Report:
            def __init__(self) -> None:
                self.started: list = []
                self.deferred: list = []
                self.held: list = []
                self.skipped: list = []

        await route_candidate(
            candidate, project=PROJECT, component_map=None, ctx=ctx, report=_Report()
        )
        return True

    result = await run_poll(
        reader=_Reader(store),
        watermark=_Watermark(),
        audit=_Audit(),
        access=_Access(),
        allowed_fields=_ALLOWED_FIELDS,
        intake=intake,
    )

    assert [event.kind for event in result.events] == [NEW]
    assert len(starter.starts) == 1
    return starter.starts[0]


def _deps_and_request(
    tmp_path, bare_remote, store: Path, start: IntakeStart
) -> tuple[PipelineDeps, PipelineRequest, dict, _FakeCodeStep]:
    transport, ghes_state = _fake_ghes()
    mediator = GitHubMediator(
        backend=_host(bare_remote, transport),
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )
    code_step = _FakeCodeStep({"README.md": "hello\npagination change\n"})
    deps = PipelineDeps(
        snapshot_store=_InMemorySnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=WorkspaceMaterializer(
            registry=InMemoryOrphanRegistry(),
            allowed_repos=frozenset({REPO}),
            workspace_root=tmp_path / "ws",
            remote_url_for=lambda repo: bare_remote,
        ),
        code_step=code_step,
        synchronizer=GitBranchSynchronizer(
            allowed_repos=frozenset({REPO}),
            remote_url_for=lambda repo: bare_remote,
        ),
        mediator=mediator,
        jira_reader=_backend(store),
        linkback_client=_backend(store),
        settings=hermetic_settings(),
        status_flow=FLOW,
    )
    request = PipelineRequest(
        task_id=start.task_id,
        execution_id=start.execution_id,
        repo=start.repo,
        base="main",
        base_sha=_remote_head(bare_remote),
        task=start.summary,
        issue_key=start.issue_key,
        raw_issue=start.stored_issue,
        jira_source="rebar",
        file_impact=start.file_impact,
    )
    return deps, request, ghes_state, code_step


async def test_a_local_ticket_reaches_a_draft_pr_and_its_store_records_the_run(
    tmp_path, bare_remote, seeded_store
) -> None:
    start = await _route_one_ticket(seeded_store)
    backend = _backend(seeded_store)
    ticket_id = start.issue_key

    assert backend.get_status(ticket_id) == "open"

    base_sha = _remote_head(bare_remote)
    deps, request, ghes_state, code_step = _deps_and_request(
        tmp_path, bare_remote, seeded_store, start
    )

    result = await run_bounded_task(deps, request)

    assert result.status == DRAFT_PR_OPEN
    assert result.branch == agent_branch_name(start.task_id)
    assert result.pr_number == 1
    assert result.linkback == APPLIED
    assert code_step.tasks == [start.summary]

    branch_head = _remote_head(bare_remote, result.branch)
    assert branch_head and branch_head != base_sha
    assert len(ghes_state["pulls"]) == 1

    # The proof the other exit tests cannot give: the local store itself carries the run.
    assert backend.get_status(ticket_id) == FLOW.ready_for_review
    assert any(result.pr_url in body for body in backend.list_comment_bodies(ticket_id))


async def test_a_local_ticket_edited_after_intake_pauses_before_the_pr_opens(
    tmp_path, bare_remote, seeded_store, monkeypatch
) -> None:
    """The pre-PR recheck reads the local store, so drift there holds the PR back."""
    import rebar

    pauses: list = []

    class _RecordingPauseStore:
        async def record(self, record) -> None:
            pauses.append(record)

    monkeypatch.setattr(
        "services.integration.jira.edits.PostgresEditPauseStore", _RecordingPauseStore
    )

    start = await _route_one_ticket(seeded_store)
    deps, request, ghes_state, _ = _deps_and_request(tmp_path, bare_remote, seeded_store, start)

    rebar.edit_ticket(
        start.issue_key,
        title="Add cursor pagination to the task list endpoint",
        repo_root=str(seeded_store),
    )

    result = await run_bounded_task(deps, request)

    assert result.status == RECHECK_PAUSED
    assert result.terminal_reason == MATERIAL_CHANGE
    assert [record.changed_fields for record in pauses] == [("status", "summary")]
    assert ghes_state["pulls"] == []
    assert not _backend(seeded_store).list_comment_bodies(start.issue_key)


@pytest.mark.skipif(
    not os.getenv("LOCAL_TICKET_LIVE_SMOKE"),
    reason="live local-ticket smoke drives a model and needs a GHES token and a seeded store",
)
async def test_local_ticket_live_smoke() -> None:
    from adapters.real.git_host import RealGitHost
    from services.integration.jira.snapshot import PostgresTaskSnapshotStore
    from services.integration.tickets.rebar_backend import build_rebar_ticket_backend
    from services.orchestration.openhands_code_step import build_openhands_code_step

    repo = os.environ["LOCAL_TICKET_LIVE_REPO"]
    ticket_id = os.environ["LOCAL_TICKET_LIVE_TICKET"]
    execution_id = os.environ.get("LOCAL_TICKET_LIVE_EXECUTION", f"live-{ticket_id}")
    base = os.environ.get("LOCAL_TICKET_LIVE_BASE", "main")

    backend = build_rebar_ticket_backend()
    ticket = backend.get_issue(ticket_id)
    candidate = candidate_from_rebar(ticket, project=backend.project, default_repo=repo)

    host = RealGitHost.from_settings()
    deps = PipelineDeps(
        snapshot_store=PostgresTaskSnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=WorkspaceMaterializer.from_settings(InMemoryOrphanRegistry()),
        code_step=build_openhands_code_step(),
        synchronizer=GitBranchSynchronizer.from_settings(),
        mediator=GitHubMediator.from_settings(
            host, InMemorySideEffectLedger(), InMemorySequenceGate()
        ),
        jira_reader=backend,
        linkback_client=backend,
        status_flow=backend.status_flow,
    )
    request = PipelineRequest(
        task_id=ticket_id,
        execution_id=execution_id,
        repo=repo,
        base=base,
        base_sha=_remote_head(host._remote_url(repo), base),
        task=candidate.summary,
        issue_key=ticket_id,
        raw_issue=candidate.stored_payload,
        jira_source="rebar",
        file_impact=candidate.file_impact,
    )

    result = await run_bounded_task(deps, request)

    assert result.status == DRAFT_PR_OPEN
    assert backend.get_status(ticket_id) == backend.status_flow.ready_for_review
