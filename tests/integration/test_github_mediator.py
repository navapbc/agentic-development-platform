"""GHES mediator over the in-memory backend and the in-process coordination surface."""

from __future__ import annotations

import pytest
from adapters.interfaces.git_host import CommitRequest, agent_branch_name
from adapters.mock.git_host import MockGitHost
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.github.mediator import (
    GitHubMediator,
    RepoNotAllowedError,
    StaleActionError,
)

REPO = "agentic-demo-service"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"


@pytest.fixture
def mediator() -> GitHubMediator:
    backend = MockGitHost()
    backend.seed_repo(REPO, files={"README.md": "hello\nworld\n"})
    return GitHubMediator(
        backend=backend,
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )


async def test_refuses_a_non_allowlisted_repository(mediator: GitHubMediator) -> None:
    with pytest.raises(RepoNotAllowedError):
        await mediator.read_file("secret-internal", "README.md", "main")


async def test_reads_and_searches_an_allowlisted_repository(mediator: GitHubMediator) -> None:
    content = await mediator.read_file(REPO, "README.md", "main")
    assert content.text == "hello\nworld\n"
    hits = await mediator.search_code(REPO, "world")
    assert [(h.path, h.line) for h in hits] == [("README.md", 2)]


async def test_ensure_agent_branch_is_deterministic_and_create_if_not_exists(
    mediator: GitHubMediator,
) -> None:
    branch = await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    assert branch.name == agent_branch_name(TASK_ID)
    assert branch.created is True

    replay = await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    assert replay.head_sha == branch.head_sha
    assert replay.created is True


async def test_commit_is_idempotent_under_a_forced_retry(mediator: GitHubMediator) -> None:
    branch = agent_branch_name(TASK_ID)
    await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    request = CommitRequest(
        repo=REPO,
        branch=branch,
        base="main",
        message="apply change",
        files={"src/app.py": "print('hi')\n"},
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
    )

    first = await mediator.commit_and_push(request, sequence=2)
    assert first.pushed is True
    assert first.replayed is False

    retry = await mediator.commit_and_push(request, sequence=2)
    assert retry == first


async def test_open_draft_pr_updates_an_existing_open_pr_instead_of_duplicating(
    mediator: GitHubMediator,
) -> None:
    branch = agent_branch_name(TASK_ID)
    await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)

    created = await mediator.open_draft_pr(
        REPO, TASK_ID, EXECUTION_ID, branch, "main", "Draft", "body", sequence=2
    )
    assert created.created is True
    assert created.draft is True

    updated = await mediator.open_draft_pr(
        REPO, TASK_ID, "exec-2", branch, "main", "Draft v2", "body v2", sequence=3
    )
    assert updated.number == created.number
    assert updated.created is False
    assert updated.title == "Draft v2"


async def test_a_stale_execution_write_is_discarded_by_the_sequence_gate(
    mediator: GitHubMediator,
) -> None:
    await mediator.ensure_agent_branch(REPO, TASK_ID, "exec-new", "main", sequence=5)
    with pytest.raises(StaleActionError):
        await mediator.ensure_agent_branch(REPO, TASK_ID, "exec-old", "main", sequence=3)


async def test_find_open_pr_returns_none_when_no_pr_exists(mediator: GitHubMediator) -> None:
    assert await mediator.find_open_pr(REPO, TASK_ID) is None


async def test_find_open_pr_locates_the_task_branch_pr(mediator: GitHubMediator) -> None:
    branch = agent_branch_name(TASK_ID)
    await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    created = await mediator.open_draft_pr(
        REPO, TASK_ID, EXECUTION_ID, branch, "main", "Draft", "body", sequence=2
    )

    found = await mediator.find_open_pr(REPO, TASK_ID)
    assert found is not None
    assert found.number == created.number
    assert found.head == branch


async def test_find_open_pr_guards_non_allowlisted_repos(mediator: GitHubMediator) -> None:
    with pytest.raises(RepoNotAllowedError):
        await mediator.find_open_pr("secret-internal", TASK_ID)


async def test_mark_pr_ready_flips_draft_and_is_idempotent(mediator: GitHubMediator) -> None:
    branch = agent_branch_name(TASK_ID)
    await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    created = await mediator.open_draft_pr(
        REPO, TASK_ID, EXECUTION_ID, branch, "main", "Draft", "body", sequence=2
    )
    assert created.draft is True

    ready = await mediator.mark_pr_ready(REPO, TASK_ID, EXECUTION_ID, created.number, sequence=3)
    assert ready.number == created.number
    assert ready.draft is False

    retry = await mediator.mark_pr_ready(REPO, TASK_ID, EXECUTION_ID, created.number, sequence=3)
    assert retry == ready


async def test_mark_pr_ready_guards_non_allowlisted_repos(mediator: GitHubMediator) -> None:
    with pytest.raises(RepoNotAllowedError):
        await mediator.mark_pr_ready("secret-internal", TASK_ID, EXECUTION_ID, 1, sequence=1)


async def test_record_promotion_approval_is_idempotent(mediator: GitHubMediator) -> None:
    branch = agent_branch_name(TASK_ID)
    await mediator.ensure_agent_branch(REPO, TASK_ID, EXECUTION_ID, "main", sequence=1)
    created = await mediator.open_draft_pr(
        REPO, TASK_ID, EXECUTION_ID, branch, "main", "Draft", "body", sequence=2
    )

    first = await mediator.record_promotion_approval(
        REPO, TASK_ID, EXECUTION_ID, created.number, approved=True, approver="rm", sequence=3
    )
    assert first == {"number": created.number, "approved": True, "approver": "rm"}

    retry = await mediator.record_promotion_approval(
        REPO, TASK_ID, EXECUTION_ID, created.number, approved=True, approver="rm", sequence=3
    )
    assert retry == first
