"""Bounded reaper for expired platform artifacts and orphaned sandbox pods, dry-run by default."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from adapters.interfaces.git_host import GitHost
from openhands.runtime.provisioner import SandboxPod

from services.coordination.orphans import (
    ARCHIVED,
    BRANCH,
    EVIDENCE,
    PR,
    REAPED,
    ExecutionRecord,
    OrphanRecord,
)
from services.orchestration.temporal_activities.persistence import AuditEvent

DELETE_BRANCH = "delete_branch"
CLOSE_PR = "close_pr"
ARCHIVE_EVIDENCE = "archive_evidence"
DELETE_POD = "delete_pod"
SKIP = "skip"

SANDBOX_POD = "sandbox_pod"
SANDBOX_POD_REAPING = "SANDBOX_POD_REAPING"

AGENT_BRANCH_PREFIX = "agent/"

NOT_PLATFORM_TAGGED = "not platform-tagged"
NOT_ALLOWLISTED = "repository not allowlisted"
NOT_AGENT_BRANCH = "not an agent branch"
UNPARSEABLE_PR = "unparseable pull request id"
UNSUPPORTED_TYPE = "unsupported artifact type"
DRY_RUN_REASON = "dry-run"
NO_EXECUTION_LABEL = "no execution label"
EXECUTION_ACTIVE = "execution active"
OTHER_TENANT_EXECUTION = "execution of another tenant"


@dataclass(frozen=True)
class ReapAction:
    orphan_id: str
    artifact_type: str
    external_id: str
    repository: str
    action: str
    applied: bool
    reason: str


@dataclass(frozen=True)
class ReapReceipt:
    dry_run: bool
    scanned: int
    actions: tuple[ReapAction, ...] = ()

    @property
    def applied(self) -> tuple[ReapAction, ...]:
        return tuple(action for action in self.actions if action.applied)


class ReapRegistry(Protocol):
    async def list_reapable(self, now: datetime, tenant: str = "") -> list[OrphanRecord]: ...

    async def mark_state(self, orphan_id: str, state: str) -> None: ...


class SandboxPodClient(Protocol):
    async def list_platform_pods(self, namespace: str) -> list[SandboxPod]: ...

    async def delete_pod(self, pod: SandboxPod) -> bool: ...


class ExecutionLookup(Protocol):
    async def find_executions(
        self, execution_ids: Collection[str]
    ) -> dict[str, ExecutionRecord]: ...


@dataclass(frozen=True)
class SandboxSweep:
    pods: SandboxPodClient
    namespace: str
    executions: ExecutionLookup
    audit: Callable[[AuditEvent], Awaitable[object]]


def _branch_name(external_id: str, repository: str) -> str:
    prefix = f"{repository}:"
    if external_id.startswith(prefix):
        return external_id[len(prefix) :]
    return external_id


def _pr_number(external_id: str, repository: str) -> int | None:
    prefix = f"{repository}#"
    tail = external_id[len(prefix) :] if external_id.startswith(prefix) else external_id
    return int(tail) if tail.isdigit() else None


async def reap(
    *,
    registry: ReapRegistry,
    backend: GitHost,
    allowed_repos: frozenset[str],
    now: datetime,
    tenant: str = "",
    dry_run: bool = True,
    sandbox: SandboxSweep | None = None,
) -> ReapReceipt:
    candidates = await registry.list_reapable(now, tenant)
    actions: list[ReapAction] = []
    # GitHub refuses to close a pull request whose head branch is already deleted.
    for record in sorted(candidates, key=lambda record: record.artifact_type != PR):
        actions.append(await _reap_one(record, registry, backend, allowed_repos, dry_run))
    scanned = len(candidates)
    if sandbox is not None:
        pod_actions = await reap_sandbox_pods(sandbox, tenant=tenant, dry_run=dry_run)
        scanned += len(pod_actions)
        actions.extend(pod_actions)
    return ReapReceipt(dry_run=dry_run, scanned=scanned, actions=tuple(actions))


async def reap_sandbox_pods(
    sweep: SandboxSweep, *, tenant: str = "", dry_run: bool = True
) -> list[ReapAction]:
    """Delete the platform-labelled pods of the sandbox namespace whose execution is not active.

    A pod is reaped when its execution has no record or has reached a terminal state, and only
    when that record, if any, belongs to this tenant. A pod without the execution label is left
    alone, because the gate, checks, and scanner pods share the namespace without one. A pod that
    fails to reap does not stop the pass, and the pass raises SandboxPodReapError at its end so
    the caller retries the failed pods.
    """
    pods = await sweep.pods.list_platform_pods(sweep.namespace)
    execution_ids = {pod.execution_id for pod in pods if pod.execution_id}
    executions = await sweep.executions.find_executions(execution_ids) if execution_ids else {}
    actions: list[ReapAction] = []
    failures: dict[str, Exception] = {}
    for pod in pods:
        try:
            actions.append(
                await _reap_pod(pod, executions.get(pod.execution_id), sweep, tenant, dry_run)
            )
        except Exception as exc:
            failures[f"{pod.namespace}/{pod.name}"] = exc
    if failures:
        raise SandboxPodReapError(failures)
    return actions


class SandboxPodReapError(RuntimeError):
    def __init__(self, failures: dict[str, Exception]) -> None:
        self.failures = failures
        detail = ", ".join(f"{pod} ({type(exc).__name__}: {exc})" for pod, exc in failures.items())
        super().__init__(f"failed to reap {len(failures)} sandbox pods: {detail}")


async def _reap_pod(
    pod: SandboxPod,
    execution: ExecutionRecord | None,
    sweep: SandboxSweep,
    tenant: str,
    dry_run: bool,
) -> ReapAction:
    if not pod.execution_id:
        return _pod_action(pod, SKIP, applied=False, reason=NO_EXECUTION_LABEL)
    if execution is not None and execution.tenant != tenant:
        return _pod_action(pod, SKIP, applied=False, reason=OTHER_TENANT_EXECUTION)
    if execution is not None and execution.active:
        return _pod_action(pod, SKIP, applied=False, reason=EXECUTION_ACTIVE)
    if dry_run:
        return _pod_action(pod, DELETE_POD, applied=False, reason=DRY_RUN_REASON)
    # The event precedes the delete because a pod deleted before a failed audit write would not
    # be listed again, so no retry could record it. Its id is fixed per pod and the insert ignores
    # conflicts, so a retry after a failed delete writes no second event.
    await sweep.audit(pod_reaping_event(pod, execution))
    deleted = await sweep.pods.delete_pod(pod)
    reason = "pod deleted" if deleted else "pod already gone or replaced"
    return _pod_action(pod, DELETE_POD, applied=True, reason=reason)


def pod_reaping_event(pod: SandboxPod, execution: ExecutionRecord | None) -> AuditEvent:
    identity = f"{SANDBOX_POD_REAPING}\x1f{pod.namespace}\x1f{pod.name}\x1f{pod.uid}"
    status = f"in state {execution.state}" if execution else "without a record"
    return AuditEvent(
        event_id=hashlib.sha256(identity.encode()).hexdigest(),
        task_id=execution.task_id if execution else "",
        execution_id=pod.execution_id,
        event_type=SANDBOX_POD_REAPING,
        decision=DELETE_POD,
        reason=f"reaping sandbox pod {pod.namespace}/{pod.name}, its execution is {status}",
    )


def _pod_action(pod: SandboxPod, action: str, *, applied: bool, reason: str) -> ReapAction:
    return ReapAction(
        orphan_id="",
        artifact_type=SANDBOX_POD,
        external_id=f"{pod.namespace}/{pod.name}",
        repository="",
        action=action,
        applied=applied,
        reason=reason,
    )


async def _reap_one(
    record: OrphanRecord,
    registry: ReapRegistry,
    backend: GitHost,
    allowed_repos: frozenset[str],
    dry_run: bool,
) -> ReapAction:
    if not record.platform_tagged:
        return _skip(record, NOT_PLATFORM_TAGGED)
    if record.repository not in allowed_repos:
        return _skip(record, NOT_ALLOWLISTED)

    if record.artifact_type == BRANCH:
        branch = _branch_name(record.external_id, record.repository)
        if not branch.startswith(AGENT_BRANCH_PREFIX):
            return _skip(record, NOT_AGENT_BRANCH)
        if dry_run:
            return _planned(record, DELETE_BRANCH)
        deleted = await backend.delete_branch(record.repository, branch)
        await registry.mark_state(record.orphan_id, REAPED)
        reason = "branch deleted" if deleted else "branch already absent"
        return _applied(record, DELETE_BRANCH, reason)

    if record.artifact_type == PR:
        number = _pr_number(record.external_id, record.repository)
        if number is None:
            return _skip(record, UNPARSEABLE_PR)
        if dry_run:
            return _planned(record, CLOSE_PR)
        await backend.close_pr(record.repository, number)
        await registry.mark_state(record.orphan_id, REAPED)
        return _applied(record, CLOSE_PR, "pull request closed")

    if record.artifact_type == EVIDENCE:
        if dry_run:
            return _planned(record, ARCHIVE_EVIDENCE)
        await registry.mark_state(record.orphan_id, ARCHIVED)
        return _applied(record, ARCHIVE_EVIDENCE, "evidence archived")

    return _skip(record, UNSUPPORTED_TYPE)


def _skip(record: OrphanRecord, reason: str) -> ReapAction:
    return ReapAction(
        orphan_id=record.orphan_id,
        artifact_type=record.artifact_type,
        external_id=record.external_id,
        repository=record.repository,
        action=SKIP,
        applied=False,
        reason=reason,
    )


def _planned(record: OrphanRecord, action: str) -> ReapAction:
    return ReapAction(
        orphan_id=record.orphan_id,
        artifact_type=record.artifact_type,
        external_id=record.external_id,
        repository=record.repository,
        action=action,
        applied=False,
        reason=DRY_RUN_REASON,
    )


def _applied(record: OrphanRecord, action: str, reason: str) -> ReapAction:
    return ReapAction(
        orphan_id=record.orphan_id,
        artifact_type=record.artifact_type,
        external_id=record.external_id,
        repository=record.repository,
        action=action,
        applied=True,
        reason=reason,
    )
