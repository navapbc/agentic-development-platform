"""Temporal activities for the REPAIRING stage: analyse a CI failure and apply a sandbox repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity


@dataclass
class AnalyzeCIFailureRequest:
    failure_summary: str
    repo: str = ""


@dataclass
class AnalyzeCIFailureResult:
    actionable: bool
    category: str
    instruction: str


@dataclass
class ApplyCIRepairRequest:
    task_id: str
    execution_id: str
    repo: str
    base: str
    commit: str
    instruction: str
    failure_summary: str = ""


@dataclass
class ApplyCIRepairResult:
    applied: bool
    changed_files: list[str] = field(default_factory=list)
    cost: float = 0.0
    tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0


@activity.defn
async def analyze_ci_failure(req: AnalyzeCIFailureRequest) -> AnalyzeCIFailureResult:
    import asyncio

    from scripts.config import get_settings

    from services.orchestration.checks.config import checks_for_repo
    from services.orchestration.langgraph.ci_repair import ACTIONABLE, analyze_failure

    settings = get_settings()
    config = checks_for_repo(req.repo, settings) if req.repo else None
    plan = await asyncio.to_thread(
        analyze_failure,
        req.failure_summary,
        use_model=settings.repair_analysis_use_llm,
        fix_command=config.fix_command if config else "",
    )
    return AnalyzeCIFailureResult(
        actionable=plan.status == ACTIONABLE,
        category=plan.category,
        instruction=plan.instruction,
    )


@activity.defn
async def apply_ci_repair(req: ApplyCIRepairRequest) -> ApplyCIRepairResult:
    from services.orchestration.temporal_activities.implementation import (
        _build_materializer,
        _build_runtime,
    )

    materializer = _build_materializer()
    workspace = await materializer.materialize(
        repo=req.repo,
        task_id=req.task_id,
        execution_id=req.execution_id,
        base=req.base,
        commit_sha=req.commit,
    )
    if await _project_autofix(req, workspace.path):
        changes = await materializer.collect_changes(workspace)
        if changes:
            return ApplyCIRepairResult(applied=True, changed_files=sorted(changes))
    runtime = _build_runtime()
    outcome = await runtime.run_implementation(
        execution_id=req.execution_id,
        task=req.instruction,
        context=req.failure_summary,
        workspace_source=str(Path(workspace.path).resolve()),
        task_id=req.task_id,
    )
    changes = await materializer.collect_changes(workspace)
    return ApplyCIRepairResult(
        applied=bool(changes),
        changed_files=sorted(changes),
        cost=outcome.cost,
        tokens=outcome.tokens,
        cache_read_tokens=outcome.cache_read_tokens,
        llm_calls=outcome.llm_calls,
    )


async def _project_autofix(req: ApplyCIRepairRequest, workspace_path: str) -> bool:
    """Run the repo's declared fix command in its own image and keep only verified, in-scope edits.

    Returns whether the fix resolved the failure. A fixer that edits files without making the
    project's checks pass is discarded, so the LLM repair still runs.
    """
    import shutil
    import tempfile

    from scripts.config import get_settings

    from services.orchestration.checks.config import checks_for_repo
    from services.orchestration.checks.runner import ChecksRunner, ChecksSpec
    from services.orchestration.gates.pod_runner import KubectlPodOps
    from services.orchestration.langgraph.ci_repair import failing_files
    from services.orchestration.temporal_activities.checks import _clean_checkout

    settings = get_settings()
    config = checks_for_repo(req.repo, settings)
    if config is None or not config.fix_command:
        return False

    checkout = tempfile.mkdtemp(prefix="fix-")
    staging = tempfile.mkdtemp(prefix="fix-out-")
    try:
        _clean_checkout(workspace_path, checkout)
        runner = ChecksRunner(
            KubectlPodOps(),
            namespace=settings.gate_engine_namespace,
            builder_image=settings.gate_engine_builder_image,
            privileged=settings.gate_engine_privileged,
        )
        result = await runner.run_fix(
            ChecksSpec(
                repo=req.repo,
                execution_id=req.execution_id,
                checkout_dir=checkout,
                command=config.command,
                fix_command=config.fix_command,
                image=config.image,
                build=config.build,
                working_dir=config.working_dir,
            ),
            staging,
        )
        if not result.verified:
            return False
        scope = failing_files(req.failure_summary)
        applied = _apply_staged_files(staging, workspace_path, result.changed_files, scope)
        return applied > 0
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)


def _apply_staged_files(
    staging: str, workspace_path: str, changed: tuple[str, ...], scope: tuple[str, ...]
) -> int:
    """Copy verified fixes back, only for files the failure named and that already existed.

    Restricting to pre-existing files keeps dependency trees and tool caches the fix command
    created inside the pod out of the repository.
    """
    import shutil

    from services.orchestration.langgraph.ci_repair import in_repair_scope

    applied = 0
    for entry in changed:
        if not in_repair_scope(entry, scope):
            continue
        source = Path(staging) / entry
        target = Path(workspace_path) / entry
        if not source.is_file() or not target.is_file():
            continue
        shutil.copyfile(source, target)
        applied += 1
    return applied
