"""Draft PR activity idempotency over the in-memory mediator and orphan registry."""

from __future__ import annotations

import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.mock.git_host import MockGitHost
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.orphans import PR, InMemoryOrphanRegistry
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.github.mediator import GitHubMediator
from services.orchestration.temporal_activities import pull_request as pr_act
from services.orchestration.temporal_activities.pull_request import OpenPrRequest, create_draft_pr

REPO = "agentic-demo-service"
TASK_ID = "AGENTDEV-1"


@pytest.fixture
def wired(monkeypatch) -> tuple[MockGitHost, InMemoryOrphanRegistry]:
    backend = MockGitHost()
    backend.seed_repo(REPO, files={"README.md": "hello\n"})
    mediator = GitHubMediator(
        backend=backend,
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )
    registry = InMemoryOrphanRegistry()
    monkeypatch.setattr(pr_act, "_build_mediator", lambda: mediator)
    monkeypatch.setattr(pr_act, "_build_registry", lambda: registry)
    return backend, registry


def _request(execution_id: str, sequence: int) -> OpenPrRequest:
    return OpenPrRequest(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=execution_id,
        head=agent_branch_name(TASK_ID, "exec-1"),
        base="main",
        title="Draft change",
        body="body",
        sequence=sequence,
        workflow_id="wf-1",
    )


async def test_forced_retry_opens_a_single_pr_and_registers_the_artifact_once(wired):
    backend, registry = wired

    first = await create_draft_pr(_request("exec-1", 2))
    retry = await create_draft_pr(_request("exec-1", 2))

    assert first.created is True
    assert first.draft is True
    assert first.number == retry.number
    assert first.orphan_id == retry.orphan_id
    assert len(backend._repos[REPO].prs) == 1
    assert len(registry.records) == 1
    artifact = next(iter(registry.records.values()))
    assert artifact.artifact_type == PR
    assert artifact.external_id == f"{REPO}#{first.number}"


async def test_a_later_execution_updates_the_existing_pr_without_duplicating(wired):
    backend, registry = wired

    first = await create_draft_pr(_request("exec-1", 2))
    later = await create_draft_pr(_request("exec-2", 3))

    assert first.created is True
    assert later.created is False
    assert later.number == first.number
    assert len(backend._repos[REPO].prs) == 1
    assert len(registry.records) == 1
