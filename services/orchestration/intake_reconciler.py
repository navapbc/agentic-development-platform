"""Level-triggered Jira intake reconciler that starts one execution per resolved repository."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from scripts.config import Settings, get_settings

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.integration.jira.credentials import (
    CLOUD,
    DATACENTER,
    JiraCredentialResolver,
    acceptance_field,
    build_jira_credential_resolver,
)
from services.integration.jira.credentials import allowed_projects as jira_allowed_projects
from services.integration.jira.intake_targets import resolve_targets
from services.integration.jira.normalize import normalize_issue
from services.integration.jira.snapshot import (
    TaskSnapshotStore,
    capture_candidate_snapshot,
    capture_task_snapshot,
    content_hash,
)
from services.integration.source_control import source_control_repos
from services.integration.tickets.rebar_backend import rebar_stored_payload
from services.orchestration.intake_base import (
    UnresolvableBaseBranch,
    declared_base,
    unresolvable_base_reason,
)
from services.orchestration.remedies import (
    base_conflict_remedy,
    checks_not_configured_remedy,
    unresolvable_base_remedy,
    unroutable_remedy,
)

_JIRA_SOURCES = frozenset({CLOUD, DATACENTER})


@dataclass(frozen=True)
class IntakeStart:
    task_id: str
    execution_id: str
    repo: str
    issue_key: str
    summary: str
    jira_project: str
    stored_issue: dict[str, Any]
    description: str = ""
    base: str = ""
    snapshot_version: int = 0
    file_impact: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntakeCandidate:
    """A ticket-neutral intake candidate that Jira and rebar sources adapt into."""

    task_id: str
    project: str
    status: str
    summary: str
    description: str
    components: tuple[str, ...]
    default_repo: str
    source_backend: str
    stored_payload: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)
    file_impact: tuple[str, ...] = ()


def candidate_from_jira(raw: dict[str, Any], *, cred: Any, field_id: str) -> IntakeCandidate:
    normalized = normalize_issue(raw, acceptance_field=field_id)
    return IntakeCandidate(
        task_id=normalized.task_id,
        project=normalized.project,
        status=normalized.status,
        summary=normalized.summary,
        description=normalized.description,
        components=normalized.components,
        default_repo=cred.default_repo,
        source_backend=cred.auth_mode,
        stored_payload=normalized.to_dict(),
        raw=raw,
    )


def declared_paths(ticket: dict[str, Any]) -> tuple[str, ...]:
    """Paths a rebar ticket declares it will change, empty when its scope is undeclared."""
    entries = ticket.get("file_impact") or []
    return tuple(
        str(entry["path"]) for entry in entries if isinstance(entry, dict) and entry.get("path")
    )


def candidate_from_rebar(
    ticket: dict[str, Any], *, project: str, default_repo: str
) -> IntakeCandidate:
    task_id = str(ticket.get("ticket_id") or "")
    repos = ticket.get("repos") or []
    components = tuple(str(name) for name in repos if name)
    payload = rebar_stored_payload(ticket, project=project)
    return IntakeCandidate(
        task_id=task_id,
        project=project,
        status=payload["status"],
        summary=payload["summary"],
        description=payload["description"],
        components=components,
        default_repo=default_repo,
        source_backend="rebar",
        stored_payload=payload,
        raw=dict(ticket),
        file_impact=declared_paths(ticket),
    )


class IntakeReadSource(Protocol):
    async def list_candidate_issues(self, project: str) -> list[dict[str, Any]]: ...


class IntakeWorkflowStarter(Protocol):
    async def start(self, start: IntakeStart) -> str: ...


class IntakeBaseResolver(Protocol):
    async def resolve_base(self, *, repo: str, branch: str) -> str: ...


class IntakeExecutionSeeder(Protocol):
    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None: ...


class IntakeActiveGuard(Protocol):
    async def active_workflow(self, *, issue_key: str, repo: str) -> str | None: ...


class IntakeRunCounter(Protocol):
    async def count_active_runs(self) -> int: ...


class IntakeAlerter(Protocol):
    async def unroutable(
        self, *, issue_key: str, project: str, reason: str, remedy: str
    ) -> None: ...


@dataclass
class RunAdmission:
    """Platform-wide admission control that bounds concurrent runs across all sources.

    Constructed per pass from the max-concurrent cap minus the current active-run
    count. Each admitted start consumes a slot. When slots are exhausted, further
    candidates are deferred for a later pass rather than started.
    """

    remaining: int

    def try_reserve(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


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
    deferred: list[str] = field(default_factory=list)


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
    admission: RunAdmission | None = None
    base_resolver: IntakeBaseResolver | None = None
    checks_repos: frozenset[str] | None = None


def checks_not_configured_reason(repos: Sequence[str]) -> str:
    return f"checks_not_configured: no project checks entry for {', '.join(sorted(repos))}"


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
    run_counter: IntakeRunCounter | None = None,
    base_resolver: IntakeBaseResolver | None = None,
    checks_repos: frozenset[str] | None = None,
) -> IntakeReport:
    resolved = settings or get_settings()
    jira_resolver = jira_resolver or build_jira_credential_resolver(resolved)
    ctx = IntakeContext(
        starter=starter,
        alerter=alerter,
        ledger=ledger,
        execution_seeder=execution_seeder,
        snapshot_store=snapshot_store,
        github_allow=frozenset(source_control_repos(resolved)),
        field_id=acceptance_field(resolved),
        settings=resolved,
        active_guard=active_guard,
        admission=await build_admission(resolved, run_counter),
        base_resolver=base_resolver,
        checks_repos=checks_repos,
    )
    report = IntakeReport()

    for project in sorted(jira_allowed_projects(resolved)):
        cred = jira_resolver.resolve(project)
        if cred is None:
            continue
        for raw in await read_source.list_candidate_issues(project):
            await process_issue(raw, project=project, cred=cred, ctx=ctx, report=report)
    return report


async def build_admission(
    settings: Settings, run_counter: IntakeRunCounter | None
) -> RunAdmission | None:
    """Build the per-pass admission window from the platform cap and active-run count."""
    if run_counter is None:
        return None
    active = await run_counter.count_active_runs()
    return RunAdmission(remaining=max(0, settings.max_concurrent_runs - active))


async def process_issue(
    raw: dict[str, Any],
    *,
    project: str,
    cred: Any,
    ctx: IntakeContext,
    report: IntakeReport,
) -> None:
    candidate = candidate_from_jira(raw, cred=cred, field_id=ctx.field_id)
    await route_candidate(
        candidate,
        project=project,
        component_map=getattr(cred, "component_repos", None),
        ctx=ctx,
        report=report,
    )


async def route_candidate(
    candidate: IntakeCandidate,
    *,
    project: str,
    component_map: Any,
    ctx: IntakeContext,
    report: IntakeReport,
) -> None:
    digest = content_hash(candidate.stored_payload)
    declaration = declared_base(candidate.stored_payload.get("labels") or [])
    if declaration.conflict:
        await _hold(
            candidate.task_id,
            project,
            declaration.conflict,
            base_conflict_remedy(candidate.task_id),
            digest,
            ctx.alerter,
            ctx.ledger,
            report,
        )
        return
    resolution = resolve_targets(
        candidate.description,
        default_repo=candidate.default_repo,
        allowlist=ctx.github_allow,
        components=candidate.components,
        component_map=component_map,
    )
    if not resolution.routable:
        await _hold(
            candidate.task_id,
            project,
            resolution.reason,
            unroutable_remedy(candidate.task_id, project),
            digest,
            ctx.alerter,
            ctx.ledger,
            report,
        )
        return
    if ctx.checks_repos is not None:
        unchecked = [repo for repo in resolution.repos if repo not in ctx.checks_repos]
        if unchecked:
            reason = checks_not_configured_reason(unchecked)
            await _hold(
                candidate.task_id,
                project,
                reason,
                checks_not_configured_remedy(candidate.task_id, unchecked),
                digest,
                ctx.alerter,
                ctx.ledger,
                report,
            )
            # The fix lands in operator config rather than the ticket, so a watermark poll
            # must re-read the ticket on the next pass instead of treating it as settled.
            report.deferred.append(candidate.task_id)
            return
    for repo in resolution.repos:
        await _start(
            candidate=candidate,
            project=project,
            repo=repo,
            digest=digest,
            base=declaration.branch,
            ctx=ctx,
            report=report,
        )


async def _hold(
    issue_key: str,
    project: str,
    reason: str,
    remedy: str,
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
        await alerter.unroutable(
            issue_key=issue_key, project=project, reason=reason, remedy=remedy
        )
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
    candidate: IntakeCandidate,
    project: str,
    repo: str,
    digest: str,
    base: str = "",
    ctx: IntakeContext,
    report: IntakeReport,
) -> None:
    issue_key = candidate.task_id
    active_guard = ctx.active_guard
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
    # Resolved before the ledger effect because a raised effect leaves its key _PENDING,
    # so validating inside _launch would re-seed an execution and snapshot on every pass.
    if (
        base
        and ctx.base_resolver is not None
        and not await ctx.base_resolver.resolve_base(repo=repo, branch=base)
    ):
        await _hold(
            issue_key,
            project,
            unresolvable_base_reason(base, repo),
            unresolvable_base_remedy(issue_key),
            digest,
            ctx.alerter,
            ctx.ledger,
            report,
        )
        return
    if ctx.admission is not None and not ctx.admission.try_reserve():
        report.deferred.append(issue_key)
        return
    ref = SideEffectRef(
        task_id=issue_key,
        execution_id="",
        operation="intake_start",
        target=f"{issue_key}:{repo}",
    )

    async def _launch() -> dict[str, str]:
        execution_id = f"intake-{uuid.uuid4().hex[:8]}"
        await ctx.execution_seeder.seed(execution_id=execution_id, task_id=issue_key, repo=repo)
        if candidate.source_backend in _JIRA_SOURCES:
            snapshot = await capture_task_snapshot(
                candidate.raw,
                execution_id=execution_id,
                jira_source=candidate.source_backend,
                settings=ctx.settings,
                store=ctx.snapshot_store,
            )
        else:
            snapshot = await capture_candidate_snapshot(
                candidate.stored_payload,
                execution_id=execution_id,
                task_id=issue_key,
                project=project,
                source=candidate.source_backend,
                store=ctx.snapshot_store,
            )
        workflow_id = await ctx.starter.start(
            IntakeStart(
                task_id=issue_key,
                execution_id=execution_id,
                repo=repo,
                issue_key=issue_key,
                summary=candidate.summary,
                description=candidate.description,
                jira_project=project,
                stored_issue=candidate.stored_payload,
                base=base,
                snapshot_version=snapshot.version,
                file_impact=candidate.file_impact,
            )
        )
        return {"workflow_id": workflow_id}

    try:
        outcome = await ctx.ledger.run_once(ref, _launch)
    except UnresolvableBaseBranch as exc:
        await _hold(
            issue_key,
            project,
            str(exc),
            unresolvable_base_remedy(issue_key),
            digest,
            ctx.alerter,
            ctx.ledger,
            report,
        )
        return
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
