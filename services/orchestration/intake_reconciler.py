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


class IntakeExecutionSeeder(Protocol):
    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None: ...


class IntakeActiveGuard(Protocol):
    async def active_workflow(self, *, issue_key: str, repo: str) -> str | None: ...


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


@dataclass(frozen=True)
class IntakeContext:
    starter: IntakeWorkflowStarter
    alerter: IntakeAlerter
    ledger: SideEffectLedger
    execution_seeder: IntakeExecutionSeeder
    snapshot_store: TaskSnapshotStore | None
    github_allow: frozenset[str]
    field_id: str
    settings: Settings
    active_guard: IntakeActiveGuard | None = None


async def reconcile_intake(
    *,
    read_source: IntakeReadSource,
    starter: IntakeWorkflowStarter,
    alerter: IntakeAlerter,
    ledger: SideEffectLedger,
    execution_seeder: IntakeExecutionSeeder,
    settings: Settings | None = None,
    jira_resolver: JiraCredentialResolver | None = None,
    snapshot_store: TaskSnapshotStore | None = None,
    active_guard: IntakeActiveGuard | None = None,
) -> IntakeReport:
    resolved = settings or get_settings()
    jira_resolver = jira_resolver or build_jira_credential_resolver(resolved)
    ctx = IntakeContext(
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        execution_seeder=execution_seeder,
        snapshot_store=snapshot_store,
        github_allow=frozenset(allowed_repos(resolved)),
        field_id=acceptance_field(resolved),
        settings=resolved,
        active_guard=active_guard,
    )
    report = IntakeReport()

    for project in sorted(jira_allowed_projects(resolved)):
        cred = jira_resolver.resolve(project)
        if cred is None:
            continue
        for raw in await read_source.list_candidate_issues(project):
            await process_issue(raw, project=project, cred=cred, ctx=ctx, report=report)
    return report


async def process_issue(
    raw: dict[str, Any],
    *,
    project: str,
    cred: Any,
    ctx: IntakeContext,
    report: IntakeReport,
) -> None:
    normalized = normalize_issue(raw, acceptance_field=ctx.field_id)
    digest = content_hash(normalized.to_dict())
    resolution = resolve_targets(
        normalized.description,
        default_repo=cred.default_repo,
        allowlist=ctx.github_allow,
        components=normalized.components,
        component_map=getattr(cred, "component_repos", None),
    )
    if not resolution.routable:
        await _hold(
            normalized.task_id, project, resolution.reason, digest, ctx.alerter, ctx.ledger, report
        )
        return
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
            starter=ctx.starter,
            ledger=ctx.ledger,
            execution_seeder=ctx.execution_seeder,
            snapshot_store=ctx.snapshot_store,
            settings=ctx.settings,
            active_guard=ctx.active_guard,
            report=report,
        )


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
    execution_seeder: IntakeExecutionSeeder,
    snapshot_store: TaskSnapshotStore | None,
    settings: Settings,
    active_guard: IntakeActiveGuard | None,
    report: IntakeReport,
) -> None:
    if active_guard is not None:
        active = await active_guard.active_workflow(issue_key=issue_key, repo=repo)
        if active is not None:
            report.started.append(
                IntakeStartRecord(
                    issue_key=issue_key,
                    project=project,
                    repo=repo,
                    workflow_id=active,
                    duplicate=True,
                )
            )
            return
    ref = SideEffectRef(
        task_id=issue_key,
        execution_id="",
        operation="intake_start",
        target=f"{digest}:{repo}",
    )

    async def _launch() -> dict[str, str]:
        execution_id = f"intake-{uuid.uuid4().hex[:8]}"
        await execution_seeder.seed(execution_id=execution_id, task_id=issue_key, repo=repo)
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
