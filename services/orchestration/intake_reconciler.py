"""Level-triggered Jira intake reconciler that starts one execution per resolved repository."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from scripts.config import Settings, get_settings

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.integration.github.credentials import allowed_repos
from services.integration.jira.credentials import (
    JiraCredentialResolver,
    acceptance_field,
    build_jira_credential_resolver,
)
from services.integration.jira.credentials import allowed_projects as jira_allowed_projects
from services.integration.jira.intake_targets import resolve_targets
from services.integration.jira.normalize import normalize_issue
from services.integration.jira.snapshot import (
    TaskSnapshotStore,
    capture_task_snapshot,
    content_hash,
)


@dataclass(frozen=True)
class IntakeStart:
    task_id: str
    execution_id: str
    repo: str
    issue_key: str
    summary: str
    jira_project: str
    stored_issue: dict[str, Any]
    base: str = "main"


class IntakeReadSource(Protocol):
    async def list_candidate_issues(self, project: str) -> list[dict[str, Any]]: ...


class IntakeWorkflowStarter(Protocol):
    async def start(self, start: IntakeStart) -> str: ...


class IntakeAlerter(Protocol):
    async def unroutable(self, *, issue_key: str, project: str, reason: str) -> None: ...


@dataclass(frozen=True)
class IntakeStartRecord:
    issue_key: str
    project: str
    repo: str
    workflow_id: str
    duplicate: bool


@dataclass(frozen=True)
class IntakeHoldRecord:
    issue_key: str
    project: str
    reason: str
    alerted: bool


@dataclass(frozen=True)
class IntakeReport:
    started: list[IntakeStartRecord] = field(default_factory=list)
    held: list[IntakeHoldRecord] = field(default_factory=list)


async def reconcile_intake(
    *,
    read_source: IntakeReadSource,
    starter: IntakeWorkflowStarter,
    alerter: IntakeAlerter,
    ledger: SideEffectLedger,
    settings: Settings | None = None,
    jira_resolver: JiraCredentialResolver | None = None,
    snapshot_store: TaskSnapshotStore | None = None,
) -> IntakeReport:
    resolved = settings or get_settings()
    jira_resolver = jira_resolver or build_jira_credential_resolver(resolved)
    github_allow = frozenset(allowed_repos(resolved))
    field_id = acceptance_field(resolved)
    report = IntakeReport()

    for project in sorted(jira_allowed_projects(resolved)):
        cred = jira_resolver.resolve(project)
        if cred is None:
            continue
        for raw in await read_source.list_candidate_issues(project):
            normalized = normalize_issue(raw, acceptance_field=field_id)
            digest = content_hash(normalized.to_dict())
            resolution = resolve_targets(
                normalized.description,
                default_repo=cred.default_repo,
                allowlist=github_allow,
            )
            if not resolution.routable:
                await _hold(
                    normalized.task_id, project, resolution.reason, digest, alerter, ledger, report
                )
                continue
            for repo in resolution.repos:
                await _start(
                    raw=raw,
                    normalized_summary=normalized.summary,
                    stored_issue=normalized.to_dict(),
                    issue_key=normalized.task_id,
                    project=project,
                    repo=repo,
                    digest=digest,
                    jira_source=cred.auth_mode,
                    starter=starter,
                    ledger=ledger,
                    snapshot_store=snapshot_store,
                    settings=resolved,
                    report=report,
                )
    return report


async def _hold(
    issue_key: str,
    project: str,
    reason: str,
    digest: str,
    alerter: IntakeAlerter,
    ledger: SideEffectLedger,
    report: IntakeReport,
) -> None:
    ref = SideEffectRef(
        task_id=issue_key,
        execution_id="",
        operation="intake_unroutable_alert",
        target=digest,
    )

    async def _send() -> dict[str, str]:
        await alerter.unroutable(issue_key=issue_key, project=project, reason=reason)
        return {"issue_key": issue_key}

    outcome = await ledger.run_once(ref, _send)
    report.held.append(
        IntakeHoldRecord(
            issue_key=issue_key,
            project=project,
            reason=reason,
            alerted=not outcome.replayed,
        )
    )


async def _start(
    *,
    raw: dict[str, Any],
    normalized_summary: str,
    stored_issue: dict[str, Any],
    issue_key: str,
    project: str,
    repo: str,
    digest: str,
    jira_source: str,
    starter: IntakeWorkflowStarter,
    ledger: SideEffectLedger,
    snapshot_store: TaskSnapshotStore | None,
    settings: Settings,
    report: IntakeReport,
) -> None:
    ref = SideEffectRef(
        task_id=issue_key,
        execution_id="",
        operation="intake_start",
        target=f"{digest}:{repo}",
    )

    async def _launch() -> dict[str, str]:
        execution_id = f"intake-{uuid.uuid4().hex[:8]}"
        await capture_task_snapshot(
            raw,
            execution_id=execution_id,
            jira_source=jira_source,
            settings=settings,
            store=snapshot_store,
        )
        workflow_id = await starter.start(
            IntakeStart(
                task_id=issue_key,
                execution_id=execution_id,
                repo=repo,
                issue_key=issue_key,
                summary=normalized_summary,
                jira_project=project,
                stored_issue=stored_issue,
            )
        )
        return {"workflow_id": workflow_id}

    outcome = await ledger.run_once(ref, _launch)
    result = outcome.result if isinstance(outcome.result, dict) else {}
    report.started.append(
        IntakeStartRecord(
            issue_key=issue_key,
            project=project,
            repo=repo,
            workflow_id=str(result.get("workflow_id", "")),
            duplicate=outcome.replayed,
        )
    )
