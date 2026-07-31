"""Bounded reaper for TTL-expired platform artifacts in allowlisted repos, dry-run by default."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from adapters.interfaces.git_host import GitHost

from services.coordination.orphans import (
    ARCHIVED,
    BRANCH,
    EVIDENCE,
    PR,
    REAPED,
    OrphanRecord,
)

DELETE_BRANCH = "delete_branch"
CLOSE_PR = "close_pr"
ARCHIVE_EVIDENCE = "archive_evidence"
SKIP = "skip"

AGENT_BRANCH_PREFIX = "agent/"

NOT_PLATFORM_TAGGED = "not platform-tagged"
NOT_ALLOWLISTED = "repository not allowlisted"
NOT_AGENT_BRANCH = "not an agent branch"
UNPARSEABLE_PR = "unparseable pull request id"
UNSUPPORTED_TYPE = "unsupported artifact type"
DRY_RUN_REASON = "dry-run"


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
    async def list_reapable(self, now: datetime) -> list[OrphanRecord]: ...

    async def mark_state(self, orphan_id: str, state: str) -> None: ...


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
    dry_run: bool = True,
) -> ReapReceipt:
    candidates = await registry.list_reapable(now)
    actions: list[ReapAction] = []
    for record in candidates:
        actions.append(await _reap_one(record, registry, backend, allowed_repos, dry_run))
    return ReapReceipt(dry_run=dry_run, scanned=len(candidates), actions=tuple(actions))


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
