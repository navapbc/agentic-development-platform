"""The guard that fails a publish whose tree is not the tree the checks ran against."""

import pytest
from adapters.interfaces.git_host import CommitRequest, CommitResult
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.github.mediator import GitHubMediator, PublishedTreeMismatch

REPO = "agentic-demo-service"
VERIFIED = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
DIVERGENT = "ffffffffffffffffffffffffffffffffffffffff"


class _Host:
    """A backend that publishes a tree of its own choosing."""

    def __init__(self, published_tree: str) -> None:
        self._published_tree = published_tree

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        return CommitResult(
            repo=request.repo,
            branch=request.branch,
            commit_sha="0f1e2d3c4b5a69788796a5b4c3d2e1f001234567",
            pushed=True,
            replayed=False,
            published_tree=self._published_tree,
        )


def _mediator(published_tree: str) -> GitHubMediator:
    return GitHubMediator(
        backend=_Host(published_tree),
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )


def _request() -> CommitRequest:
    return CommitRequest(
        repo=REPO,
        branch="agent/AGENTDEV-1",
        base="main",
        message="apply change",
        workspace="",
        tree=VERIFIED,
        task_id="AGENTDEV-1",
        execution_id="exec-1",
    )


async def test_a_matching_tree_passes() -> None:
    result = await _mediator(VERIFIED).commit_and_push(_request(), sequence=1)
    assert result.published_tree == VERIFIED


async def test_a_divergent_tree_fails_the_publish() -> None:
    with pytest.raises(PublishedTreeMismatch) as raised:
        await _mediator(DIVERGENT).commit_and_push(_request(), sequence=1)
    message = str(raised.value)
    assert VERIFIED in message
    assert DIVERGENT in message
