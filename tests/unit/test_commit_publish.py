"""The guard that fails a publish whose tree is not the tree the checks ran against, and the
commit activity that reports or holds files outside the ticket's file impact."""

from dataclasses import replace
from typing import cast

import pytest
from adapters.interfaces.git_host import CommitRequest, CommitResult, GitHost
from services.coordination.claims import InMemoryWorkClaimStore, WorkUnit
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.github.mediator import GitHubMediator, PublishedTreeMismatch
from services.orchestration.materialization import (
    MaterializedWorkspace,
    StagedChange,
    WorkspaceMaterializer,
)
from services.orchestration.temporal_activities.pull_request import (
    CommitPushRequest,
    commit_and_push,
)

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


def _mediator(
    published_tree: str, *, gate: InMemorySequenceGate | None = None, tenant: str = ""
) -> GitHubMediator:
    return GitHubMediator(
        backend=cast(GitHost, _Host(published_tree)),
        ledger=InMemorySideEffectLedger(),
        gate=gate or InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
        tenant=tenant,
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


async def test_two_tenants_publish_the_same_ticket_through_one_sequence_gate() -> None:
    gate = InMemorySequenceGate()
    await _mediator(VERIFIED, gate=gate, tenant="alpha").commit_and_push(_request(), sequence=5)

    result = await _mediator(VERIFIED, gate=gate, tenant="beta").commit_and_push(
        _request(), sequence=1
    )

    assert result.published_tree == VERIFIED


class _RecordingHost(_Host):
    def __init__(self) -> None:
        super().__init__(VERIFIED)
        self.published: list[CommitRequest] = []

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        self.published.append(request)
        return await super().commit_and_push(request)


class _Materializer:
    """Stages a workspace whose change includes one file outside the file impact."""

    def __init__(self, path: str) -> None:
        self._path = path

    async def materialize(self, **kwargs) -> MaterializedWorkspace:
        return MaterializedWorkspace(
            repo=REPO,
            task_id=kwargs["task_id"],
            execution_id=kwargs["execution_id"],
            branch="agent/AGENTDEV-1",
            base=kwargs["base"],
            commit_sha=kwargs["commit_sha"],
            path=self._path,
            branch_orphan_id="",
            workspace_orphan_id="",
        )

    async def stage(self, workspace, *, file_impact) -> StagedChange:
        return StagedChange(
            tree=VERIFIED, changed=True, outside_file_impact=("tsconfig.build.json",)
        )

    async def build_commit_request(self, workspace, *, message, staged) -> CommitRequest:
        return replace(_request(), tree=staged.tree, message=message)


@pytest.fixture
def commit_activity(monkeypatch, tmp_path):
    from services.orchestration.temporal_activities import implementation, pull_request

    host = _RecordingHost()
    claims = InMemoryWorkClaimStore()
    monkeypatch.setattr(
        WorkspaceMaterializer,
        "from_settings",
        classmethod(lambda cls, registry: _Materializer(str(tmp_path))),
    )
    monkeypatch.setattr(
        pull_request,
        "_build_mediator",
        lambda: GitHubMediator(
            backend=cast(GitHost, host),
            ledger=InMemorySideEffectLedger(),
            gate=InMemorySequenceGate(),
            allowed_repos=frozenset({REPO}),
        ),
    )
    monkeypatch.setattr(implementation, "_build_coordination_client", lambda: claims)
    return host, claims


def _commit_push_request(*, check_claims: bool = True) -> CommitPushRequest:
    return CommitPushRequest(
        repo=REPO,
        task_id="AGENTDEV-1",
        execution_id="exec-1",
        base="main",
        commit="a" * 40,
        message="apply change",
        file_impact=["src/store.ts"],
        work_unit="unit-1",
        check_claims=check_claims,
    )


async def test_a_commit_includes_a_file_outside_the_file_impact_and_names_it(commit_activity):
    host, _claims = commit_activity

    result = await commit_and_push(_commit_push_request())

    assert result.pushed is True
    assert result.held_path == ""
    assert result.excluded_paths == ["tsconfig.build.json"]
    assert [request.tree for request in host.published] == [VERIFIED]


async def test_a_file_another_active_unit_holds_is_neither_committed_nor_pushed(commit_activity):
    host, claims = commit_activity
    await claims.try_claim(
        WorkUnit("AGENTDEV-2", "unit-1", "exec-2", "agent-b", ("tsconfig.build.json",), "wf-2")
    )

    result = await commit_and_push(_commit_push_request())

    assert result.pushed is False
    assert result.commit_sha == "a" * 40
    assert (result.held_path, result.held_by_task_id) == ("tsconfig.build.json", "AGENTDEV-2")
    assert host.published == []


async def test_a_run_recorded_before_the_claim_check_commits_a_held_file(commit_activity):
    host, claims = commit_activity
    await claims.try_claim(
        WorkUnit("AGENTDEV-2", "unit-1", "exec-2", "agent-b", ("tsconfig.build.json",), "wf-2")
    )

    result = await commit_and_push(_commit_push_request(check_claims=False))

    assert result.pushed is True
    assert result.held_path == ""
    assert len(host.published) == 1
