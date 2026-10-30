"""Temporal activity for planning-phase cross-ticket dependency detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity


@dataclass
class DetectDependencyRequest:
    repo: str
    task_id: str
    execution_id: str
    issue_key: str
    context_package: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectDependencyResult:
    detected: bool = False
    active: bool = False
    blocking_task_id: str = ""
    blocking_execution_id: str = ""
    reason: str = ""
    overlap: list[str] = field(default_factory=list)


@activity.defn
async def detect_dependency(req: DetectDependencyRequest) -> DetectDependencyResult:
    from services.integration.tickets import (
        DependencyBackend,
        build_ticket_backend_for_issue,
    )
    from services.orchestration.dependency_detection import (
        PeerRun,
        predicted_touch_set,
        unresolved_symbol_files,
    )
    from services.orchestration.dependency_detection import (
        detect_dependency as run_detection,
    )
    from services.orchestration.file_impact_store import FileImpactStore
    from services.orchestration.intake_runtime import PostgresExecutionSeeder

    if not req.issue_key:
        return DetectDependencyResult(detected=False, active=False)

    store = FileImpactStore()
    touch = predicted_touch_set(req.context_package)
    symbol_files = unresolved_symbol_files(req.context_package)
    await store.write(req.task_id, touch, req.execution_id or req.task_id)

    backend = build_ticket_backend_for_issue(req.issue_key)
    if not isinstance(backend, DependencyBackend) or not backend.dependency_graph_active():
        return DetectDependencyResult(detected=False, active=False)

    peers: list[PeerRun] = []
    for execution in await PostgresExecutionSeeder().list_active_executions():
        if execution.execution_id == req.execution_id or execution.task_id == req.task_id:
            continue
        impact = await store.read(execution.task_id)
        peers.append(
            PeerRun(
                execution_id=execution.execution_id,
                task_id=execution.task_id,
                file_impact=tuple(impact),
            )
        )

    finding = run_detection(touch_set=touch, symbol_files=symbol_files, peers=peers)
    if finding is None:
        return DetectDependencyResult(detected=False, active=True)
    return DetectDependencyResult(
        detected=True,
        active=True,
        blocking_task_id=finding.blocking_task_id,
        blocking_execution_id=finding.blocking_execution_id,
        reason=finding.reason,
        overlap=list(finding.overlap),
    )


@dataclass
class SignalDependencyReadyRequest:
    blocking_task_id: str
    blocking_issue_key: str


@dataclass
class CaptureBaseHeadRequest:
    repo: str
    base: str


@dataclass
class CaptureBaseHeadResult:
    base_sha: str


@dataclass
class CheckDependencyCycleRequest:
    dependent_task_id: str
    blocking_issue_key: str
    blocking_task_id: str


@dataclass
class CheckDependencyCycleResult:
    is_cycle: bool = False


@activity.defn
async def check_dependency_cycle(
    req: CheckDependencyCycleRequest,
) -> CheckDependencyCycleResult:
    from services.integration.tickets import (
        DependencyBackend,
        build_ticket_backend_for_issue,
    )

    if not req.blocking_issue_key or not req.dependent_task_id:
        return CheckDependencyCycleResult(False)
    backend = build_ticket_backend_for_issue(req.blocking_issue_key)
    if not isinstance(backend, DependencyBackend) or not backend.dependency_graph_active():
        return CheckDependencyCycleResult(False)
    seen: set[str] = set()
    frontier = [req.blocking_task_id]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        for blocker in backend.declared_dependencies(current):
            if blocker == req.dependent_task_id:
                return CheckDependencyCycleResult(True)
            frontier.append(blocker)
    return CheckDependencyCycleResult(False)


@activity.defn
async def capture_base_head(req: CaptureBaseHeadRequest) -> CaptureBaseHeadResult:
    from scripts.config import get_settings

    from services.integration.github.factory import build_git_auth_for, build_git_host
    from services.orchestration.intake_runtime import remote_head

    settings = get_settings()
    location = build_git_host(settings).source_location(req.repo)
    auth = build_git_auth_for(settings)(req.repo)
    return CaptureBaseHeadResult(base_sha=remote_head(location, req.base, auth))


@dataclass
class SignalDependencyReadyResult:
    signaled: int = 0


@activity.defn
async def signal_dependency_ready(
    req: SignalDependencyReadyRequest,
) -> SignalDependencyReadyResult:
    from scripts.config import get_settings
    from temporalio.client import Client

    from services.integration.jira.adapter import project_of
    from services.integration.tickets import (
        DependencyBackend,
        build_ticket_backend_for_issue,
        resolve_status_flow,
    )
    from services.orchestration.intake_runtime import PostgresExecutionSeeder
    from services.orchestration.suspension import RESUME_SIGNAL, ResumeOn
    from services.orchestration.terminal_guard import blocking_ticket_present_in_base

    if not req.blocking_issue_key:
        return SignalDependencyReadyResult(0)
    backend = build_ticket_backend_for_issue(req.blocking_issue_key)
    if not isinstance(backend, DependencyBackend) or not backend.dependency_graph_active():
        return SignalDependencyReadyResult(0)

    flow = resolve_status_flow(project=project_of(req.blocking_issue_key))
    status = backend.get_status(req.blocking_issue_key)
    if not blocking_ticket_present_in_base(status, flow.terminal):
        return SignalDependencyReadyResult(0)

    dependent_ids = set(backend.dependents(req.blocking_task_id))
    if not dependent_ids:
        return SignalDependencyReadyResult(0)

    settings = get_settings()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    signaled = 0
    for execution in await PostgresExecutionSeeder().list_active_executions():
        if execution.task_id in dependent_ids and execution.workflow_id:
            handle = client.get_workflow_handle(execution.workflow_id)
            await handle.signal(RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)
            signaled += 1
    return SignalDependencyReadyResult(signaled)
