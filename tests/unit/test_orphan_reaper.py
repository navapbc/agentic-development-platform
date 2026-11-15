"""Unit tests for the orphan reaper logic against the mock git host."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from adapters.interfaces.git_host import agent_branch_name
from adapters.mock.git_host import MockGitHost
from services.coordination.orphans import (
    ARCHIVED,
    BRANCH,
    EVIDENCE,
    PR,
    REAPED,
    InMemoryOrphanRegistry,
    OrphanArtifact,
    orphan_id_for,
)
from services.coordination.reaper import (
    ARCHIVE_EVIDENCE,
    CLOSE_PR,
    DELETE_BRANCH,
    NOT_ALLOWLISTED,
    NOT_PLATFORM_TAGGED,
    SKIP,
    reap,
)

REPO = "agentic-demo-service"
ALLOWED = frozenset({REPO})
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PAST = NOW - timedelta(hours=1)
FUTURE = NOW + timedelta(hours=1)


def _branch() -> str:
    return agent_branch_name("AGENTDEV-1")


async def _seed_branch(backend: MockGitHost) -> str:
    backend.seed_repo(REPO, files={"README.md": "hello\n"})
    branch = _branch()
    await backend.ensure_branch(REPO, branch, "main")
    return branch


async def test_dry_run_plans_branch_and_pr_without_side_effects() -> None:
    backend = MockGitHost()
    branch = await _seed_branch(backend)
    pr = await backend.open_draft_pr(REPO, branch, "main", "title", "body")
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST)
    )
    await registry.register(OrphanArtifact(PR, f"{REPO}#{pr.number}", repository=REPO, ttl_at=PAST))

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=True
    )

    assert receipt.dry_run is True
    assert receipt.scanned == 2
    assert receipt.applied == ()
    actions = {a.action for a in receipt.actions}
    assert actions == {DELETE_BRANCH, CLOSE_PR}
    assert branch in backend._repos[REPO].heads
    feedback = await backend.read_pr_feedback(REPO, pr.number)
    assert feedback.state == "open"


async def test_apply_deletes_branch_closes_pr_and_marks_reaped() -> None:
    backend = MockGitHost()
    branch = await _seed_branch(backend)
    pr = await backend.open_draft_pr(REPO, branch, "main", "title", "body")
    registry = InMemoryOrphanRegistry()
    branch_id = await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST)
    )
    pr_id = await registry.register(
        OrphanArtifact(PR, f"{REPO}#{pr.number}", repository=REPO, ttl_at=PAST)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert len(receipt.applied) == 2
    assert branch not in backend._repos[REPO].heads
    feedback = await backend.read_pr_feedback(REPO, pr.number)
    assert feedback.state == "closed"
    assert registry.states[branch_id] == REAPED
    assert registry.states[pr_id] == REAPED
    assert await registry.list_reapable(NOW) == []


async def test_refuses_non_platform_tagged_artifact() -> None:
    backend = MockGitHost()
    branch = await _seed_branch(backend)
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(
            BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST, platform_tagged=False
        )
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert receipt.applied == ()
    assert receipt.actions[0].action == SKIP
    assert receipt.actions[0].reason == NOT_PLATFORM_TAGGED
    assert branch in backend._repos[REPO].heads


async def test_refuses_non_allowlisted_repository() -> None:
    backend = MockGitHost()
    await _seed_branch(backend)
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, "other-repo:agent/x/y", repository="other-repo", ttl_at=PAST)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert receipt.applied == ()
    assert receipt.actions[0].action == SKIP
    assert receipt.actions[0].reason == NOT_ALLOWLISTED


async def test_archives_expired_evidence() -> None:
    backend = MockGitHost()
    registry = InMemoryOrphanRegistry()
    evidence_id = await registry.register(
        OrphanArtifact(EVIDENCE, "evidence-123", repository=REPO, ttl_at=PAST)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert len(receipt.applied) == 1
    assert receipt.actions[0].action == ARCHIVE_EVIDENCE
    assert registry.states[evidence_id] == ARCHIVED


async def test_unexpired_artifacts_are_not_scanned() -> None:
    backend = MockGitHost()
    branch = await _seed_branch(backend)
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=FUTURE)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert receipt.scanned == 0
    assert branch in backend._repos[REPO].heads
    assert orphan_id_for(BRANCH, f"{REPO}:{branch}", REPO) in registry.records


async def test_list_reapable_filters_by_tenant() -> None:
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:agent/AGENTDEV-1", repository=REPO, ttl_at=PAST)
    )
    await registry.register(
        OrphanArtifact(
            BRANCH, f"{REPO}:agent/beta/AGENTDEV-1", repository=REPO, ttl_at=PAST, tenant="beta"
        )
    )

    platform = await registry.list_reapable(NOW)
    beta = await registry.list_reapable(NOW, "beta")

    assert [r.external_id for r in platform] == [f"{REPO}:agent/AGENTDEV-1"]
    assert [r.external_id for r in beta] == [f"{REPO}:agent/beta/AGENTDEV-1"]


async def test_reaper_leaves_another_tenants_branch_alone() -> None:
    backend = MockGitHost()
    backend.seed_repo(REPO, files={"README.md": "hello\n"})
    alpha_branch = agent_branch_name("AGENTDEV-1", "alpha")
    beta_branch = agent_branch_name("AGENTDEV-1", "beta")
    await backend.ensure_branch(REPO, alpha_branch, "main")
    await backend.ensure_branch(REPO, beta_branch, "main")
    registry = InMemoryOrphanRegistry()
    alpha_id = await registry.register(
        OrphanArtifact(
            BRANCH, f"{REPO}:{alpha_branch}", repository=REPO, ttl_at=PAST, tenant="alpha"
        )
    )
    beta_id = await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{beta_branch}", repository=REPO, ttl_at=PAST, tenant="beta")
    )

    receipt = await reap(
        registry=registry,
        backend=backend,
        allowed_repos=ALLOWED,
        now=NOW,
        tenant="alpha",
        dry_run=False,
    )

    assert [a.orphan_id for a in receipt.applied] == [alpha_id]
    assert alpha_branch not in backend._repos[REPO].heads
    assert beta_branch in backend._repos[REPO].heads
    assert registry.states[alpha_id] == REAPED
    assert registry.states[beta_id] != REAPED


async def test_apply_closes_pull_requests_before_deleting_branches() -> None:
    calls: list[str] = []

    class OrderedHost(MockGitHost):
        async def delete_branch(self, repo: str, name: str) -> bool:
            calls.append(DELETE_BRANCH)
            return await super().delete_branch(repo, name)

        async def close_pr(self, repo: str, number: int):
            calls.append(CLOSE_PR)
            return await super().close_pr(repo, number)

    backend = OrderedHost()
    branch = await _seed_branch(backend)
    pr = await backend.open_draft_pr(REPO, branch, "main", "title", "body")
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST)
    )
    await registry.register(OrphanArtifact(PR, f"{REPO}#{pr.number}", repository=REPO, ttl_at=PAST))

    await reap(registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False)

    assert calls == [CLOSE_PR, DELETE_BRANCH]
