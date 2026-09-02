"""Temporal activities for the REPAIRING stage: analyse a CI failure and apply a sandbox repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity


@dataclass
class AnalyzeCIFailureRequest:
    failure_summary: str


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
    llm_calls: int = 0


@activity.defn
async def analyze_ci_failure(req: AnalyzeCIFailureRequest) -> AnalyzeCIFailureResult:
    import asyncio

    from scripts.config import get_settings

    from services.orchestration.langgraph.ci_repair import ACTIONABLE, analyze_failure

    plan = await asyncio.to_thread(
        analyze_failure, req.failure_summary, use_model=get_settings().repair_analysis_use_llm
    )
    return AnalyzeCIFailureResult(
        actionable=plan.status == ACTIONABLE,
        category=plan.category,
        instruction=plan.instruction,
    )


@activity.defn
async def apply_ci_repair(req: ApplyCIRepairRequest) -> ApplyCIRepairResult:
    from services.orchestration.langgraph.ci_repair import LINT, classify_failure
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
    if classify_failure(req.failure_summary) == LINT and await _autofix_lint(workspace.path):
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
        llm_calls=outcome.llm_calls,
    )


async def _autofix_lint(path: str) -> bool:
    """Apply ruff's safe auto-fixes in the workspace. Returns whether ruff ran."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff",
            "check",
            "--fix",
            ".",
            cwd=path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    await proc.wait()
    return True
