"""Unit tests for the orphan reaper against the mock git host and a fake Kubernetes API."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.mock.git_host import MockGitHost
from openhands.runtime.provisioner import (
    KubernetesSandboxPods,
    SandboxSpec,
    build_pod_manifest,
    pod_name_for,
)
from services.coordination.orphans import (
    ARCHIVED,
    BRANCH,
    EVIDENCE,
    PR,
    REAPED,
    ExecutionRecord,
    InMemoryOrphanRegistry,
    OrphanArtifact,
    orphan_id_for,
)
from services.coordination.reaper import (
    ARCHIVE_EVIDENCE,
    CLOSE_PR,
    DELETE_BRANCH,
    DELETE_POD,
    EXECUTION_ACTIVE,
    NO_EXECUTION_LABEL,
    NOT_ALLOWLISTED,
    NOT_PLATFORM_TAGGED,
    OTHER_TENANT_EXECUTION,
    SANDBOX_POD,
    SANDBOX_POD_REAPING,
    SKIP,
    SandboxPodReapError,
    SandboxSweep,
    reap,
)
from services.orchestration.temporal_activities.persistence import AuditEvent

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


SANDBOX_NAMESPACE = "tenant-alpha-sandbox"


class _FakePodCore:
    """Keeps pods by namespace and name, honouring label selectors and uid preconditions."""

    def __init__(self) -> None:
        self.pods: dict[tuple[str, str], SimpleNamespace] = {}
        self.failing_deletes: set[str] = set()
        self._uids = itertools.count(1)

    def create_namespaced_pod(self, namespace: str, manifest: dict) -> None:
        metadata = manifest["metadata"]
        self.pods[(namespace, metadata["name"])] = SimpleNamespace(
            metadata=SimpleNamespace(
                name=metadata["name"],
                labels=dict(metadata.get("labels") or {}),
                uid=f"uid-{next(self._uids)}",
            )
        )

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> SimpleNamespace:
        key, _, value = label_selector.partition("=")
        return SimpleNamespace(
            items=[
                pod
                for (pod_namespace, _), pod in self.pods.items()
                if pod_namespace == namespace and pod.metadata.labels.get(key) == value
            ]
        )

    def delete_namespaced_pod(self, name: str, namespace: str, body=None) -> None:
        from kubernetes.client.exceptions import ApiException

        if name in self.failing_deletes:
            raise ApiException(status=500)
        pod = self.pods.get((namespace, name))
        if pod is None:
            raise ApiException(status=404)
        expected = body.preconditions.uid if body is not None and body.preconditions else None
        if expected is not None and expected != pod.metadata.uid:
            raise ApiException(status=409)
        del self.pods[(namespace, name)]

    def names(self, namespace: str = SANDBOX_NAMESPACE) -> set[str]:
        return {name for pod_namespace, name in self.pods if pod_namespace == namespace}


def _create_sandbox_pod(core: _FakePodCore, execution_id: str, namespace: str = SANDBOX_NAMESPACE):
    core.create_namespaced_pod(
        namespace,
        build_pod_manifest(
            SandboxSpec(execution_id=execution_id, image="img", namespace=namespace)
        ),
    )


def _sweep(
    core: _FakePodCore,
    registry: InMemoryOrphanRegistry,
    audit: list[AuditEvent],
    fails: Callable[[AuditEvent], bool] = lambda event: False,
):
    async def _record(event: AuditEvent) -> str:
        if fails(event):
            raise ConnectionError("audit store unavailable")
        audit.append(event)
        return event.event_id

    pods = KubernetesSandboxPods(in_cluster=False)
    pods._core = core
    return SandboxSweep(pods=pods, namespace=SANDBOX_NAMESPACE, executions=registry, audit=_record)


async def test_one_reaper_run_deletes_the_pod_of_an_execution_that_does_not_exist() -> None:
    core = _FakePodCore()
    _create_sandbox_pod(core, "exec-missing")
    registry = InMemoryOrphanRegistry()
    audit: list[AuditEvent] = []

    receipt = await reap(
        registry=registry,
        backend=MockGitHost(),
        allowed_repos=ALLOWED,
        now=NOW,
        tenant="alpha",
        dry_run=False,
        sandbox=_sweep(core, registry, audit),
    )

    assert core.names() == set()
    assert [(a.artifact_type, a.action, a.applied) for a in receipt.actions] == [
        (SANDBOX_POD, DELETE_POD, True)
    ]
    assert [(e.event_type, e.execution_id, e.task_id) for e in audit] == [
        (SANDBOX_POD_REAPING, "exec-missing", "")
    ]


async def test_the_reaper_deletes_only_the_pods_of_its_tenant_without_an_active_execution() -> None:
    core = _FakePodCore()
    for execution_id in ("exec-active", "exec-done", "exec-missing", "exec-beta"):
        _create_sandbox_pod(core, execution_id)
    _create_sandbox_pod(core, "exec-other-namespace", namespace="tenant-beta-sandbox")
    core.create_namespaced_pod(
        SANDBOX_NAMESPACE,
        {
            "metadata": {
                "name": "gate-engine-run",
                "labels": {
                    "app": "gate-engine",
                    "app.kubernetes.io/part-of": "agentic-dev-platform",
                },
            }
        },
    )
    registry = InMemoryOrphanRegistry()
    registry.executions = {
        "exec-active": ExecutionRecord("exec-active", "ADSER-1", "IMPLEMENTING", "alpha"),
        "exec-done": ExecutionRecord("exec-done", "ADSER-2", "COMPLETED", "alpha"),
        "exec-beta": ExecutionRecord("exec-beta", "ADSER-3", "FAILED", "beta"),
    }
    audit: list[AuditEvent] = []

    receipt = await reap(
        registry=registry,
        backend=MockGitHost(),
        allowed_repos=ALLOWED,
        now=NOW,
        tenant="alpha",
        dry_run=False,
        sandbox=_sweep(core, registry, audit),
    )

    assert core.names() == {
        pod_name_for("exec-active"),
        pod_name_for("exec-beta"),
        "gate-engine-run",
    }
    assert core.names("tenant-beta-sandbox") == {pod_name_for("exec-other-namespace")}
    assert {(e.execution_id, e.task_id) for e in audit} == {
        ("exec-done", "ADSER-2"),
        ("exec-missing", ""),
    }
    assert all(e.event_type == SANDBOX_POD_REAPING for e in audit)
    skipped = {a.external_id: a.reason for a in receipt.actions if a.action == SKIP}
    assert skipped == {
        f"{SANDBOX_NAMESPACE}/{pod_name_for('exec-active')}": EXECUTION_ACTIVE,
        f"{SANDBOX_NAMESPACE}/{pod_name_for('exec-beta')}": OTHER_TENANT_EXECUTION,
        f"{SANDBOX_NAMESPACE}/gate-engine-run": NO_EXECUTION_LABEL,
    }


async def test_a_dry_run_plans_pod_deletions_without_deleting_or_auditing() -> None:
    core = _FakePodCore()
    _create_sandbox_pod(core, "exec-missing")
    registry = InMemoryOrphanRegistry()
    audit: list[AuditEvent] = []

    receipt = await reap(
        registry=registry,
        backend=MockGitHost(),
        allowed_repos=ALLOWED,
        now=NOW,
        tenant="alpha",
        dry_run=True,
        sandbox=_sweep(core, registry, audit),
    )

    assert core.names() == {pod_name_for("exec-missing")}
    assert audit == []
    assert [(a.action, a.applied) for a in receipt.actions] == [(DELETE_POD, False)]


async def test_a_pod_recreated_under_the_listed_name_is_not_deleted() -> None:
    core = _FakePodCore()
    _create_sandbox_pod(core, "exec-1")
    pods = KubernetesSandboxPods(in_cluster=False)
    pods._core = core
    [listed] = await pods.list_platform_pods(SANDBOX_NAMESPACE)
    _create_sandbox_pod(core, "exec-1")

    assert await pods.delete_pod(listed) is False
    assert core.names() == {pod_name_for("exec-1")}


async def _apply(sweep: SandboxSweep):
    return await reap(
        registry=InMemoryOrphanRegistry(),
        backend=MockGitHost(),
        allowed_repos=ALLOWED,
        now=NOW,
        tenant="alpha",
        dry_run=False,
        sandbox=sweep,
    )


async def test_an_audit_failure_keeps_its_pod_and_the_sweep_reaps_the_others() -> None:
    core = _FakePodCore()
    for execution_id in ("exec-a", "exec-b", "exec-c"):
        _create_sandbox_pod(core, execution_id)
    registry = InMemoryOrphanRegistry()
    audit: list[AuditEvent] = []

    with pytest.raises(SandboxPodReapError) as raised:
        await _apply(_sweep(core, registry, audit, fails=lambda e: e.execution_id == "exec-b"))

    assert list(raised.value.failures) == [f"{SANDBOX_NAMESPACE}/{pod_name_for('exec-b')}"]
    assert core.names() == {pod_name_for("exec-b")}
    assert [e.execution_id for e in audit] == ["exec-a", "exec-c"]

    await _apply(_sweep(core, registry, audit))

    assert core.names() == set()
    assert [e.execution_id for e in audit] == ["exec-a", "exec-c", "exec-b"]


async def test_a_delete_failure_is_retried_under_the_same_audit_event() -> None:
    core = _FakePodCore()
    for execution_id in ("exec-a", "exec-b", "exec-c"):
        _create_sandbox_pod(core, execution_id)
    core.failing_deletes.add(pod_name_for("exec-b"))
    registry = InMemoryOrphanRegistry()
    audit: list[AuditEvent] = []

    with pytest.raises(SandboxPodReapError) as raised:
        await _apply(_sweep(core, registry, audit))

    assert list(raised.value.failures) == [f"{SANDBOX_NAMESPACE}/{pod_name_for('exec-b')}"]
    assert core.names() == {pod_name_for("exec-b")}
    assert all(e.reason and e.reason.startswith("reaping sandbox pod") for e in audit)
    first_event = next(e.event_id for e in audit if e.execution_id == "exec-b")

    core.failing_deletes.clear()
    await _apply(_sweep(core, registry, audit))

    assert core.names() == set()
    assert [e.event_id for e in audit if e.execution_id == "exec-b"] == [first_event] * 2
