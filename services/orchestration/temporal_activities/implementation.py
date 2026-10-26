"""Temporal activities for the IMPLEMENTING execution stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio import activity

from services.prompts.library import IMPLEMENTATION_VERSION

IMPLEMENTATION_PROMPT_VERSION = IMPLEMENTATION_VERSION


@dataclass
class ClaimRequest:
    task_id: str
    execution_id: str
    work_unit: str
    agent_id: str
    file_impact: list[str] = field(default_factory=list)
    owner_workflow_id: str = ""


@dataclass
class ClaimResult:
    claim_id: str
    won: bool
    claim_version: int
    owner_workflow_id: str = ""


@dataclass
class ResetCoordinationRequest:
    task_id: str
    work_unit: str


@dataclass
class ResetCoordinationResult:
    sequence_reset: bool


@dataclass
class FinalizeClaimRequest:
    task_id: str
    work_unit: str
    owner_workflow_id: str
    completed: bool


@dataclass
class FinalizeClaimResult:
    claim_id: str
    finalized: bool
    state: str


@dataclass
class ImplementationRequest:
    task_id: str
    execution_id: str
    repo: str
    base: str
    commit: str
    task: str
    context: str = ""
    context_package: dict[str, Any] = field(default_factory=dict)
    plan: str = ""


@dataclass
class ImplementationResult:
    status: str
    terminal_reason: str
    tool_calls: int
    elapsed_seconds: float
    cost: float
    tokens: int
    llm_calls: int
    branch: str
    commit_sha: str
    diff_hash: str
    model_id: str
    prompt_version: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    workspace_path: str = ""
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""


def _build_coordination_client():
    from services.orchestration.coordination_client import CoordinationClient

    return CoordinationClient()


def _build_materializer():
    from services.coordination.orphans import PostgresOrphanRegistry
    from services.orchestration.materialization import WorkspaceMaterializer

    return WorkspaceMaterializer.from_settings(PostgresOrphanRegistry())


def _build_runtime():
    from openhands.runtime.conversation import RemoteConversationDriver
    from openhands.runtime.provisioner import (
        KubernetesSandboxProvisioner,
        PortForwardSandboxProvisioner,
    )
    from openhands.runtime.runtime import SandboxRuntime
    from scripts.config import get_settings

    settings = get_settings()
    litellm_base_url = settings.sandbox_litellm_base_url
    driver = RemoteConversationDriver(
        model=settings.coding_model,
        litellm_base_url=litellm_base_url,
        api_key=settings.litellm_master_key,
    )
    inner = KubernetesSandboxProvisioner(
        in_cluster=settings.openhands_in_cluster,
        ready_timeout_seconds=600.0,
    )
    provisioner = inner if settings.openhands_in_cluster else PortForwardSandboxProvisioner(inner)
    runtime = SandboxRuntime.from_settings(provisioner, driver, settings)
    runtime.litellm_base_url = litellm_base_url
    return runtime


@activity.defn
async def claim_work_unit(req: ClaimRequest) -> ClaimResult:
    from services.coordination.claims import WorkUnit

    owner_workflow_id = req.owner_workflow_id or activity.info().workflow_id
    client = _build_coordination_client()
    outcome = await client.try_claim(
        WorkUnit(
            task_id=req.task_id,
            work_unit=req.work_unit,
            execution_id=req.execution_id,
            agent_id=req.agent_id,
            file_impact=tuple(req.file_impact),
            workflow_id=owner_workflow_id,
        )
    )
    return ClaimResult(
        claim_id=outcome.claim_id,
        won=outcome.won,
        claim_version=outcome.claim_version,
        owner_workflow_id=owner_workflow_id if outcome.won else "",
    )


@activity.defn
async def reset_task_coordination(req: ResetCoordinationRequest) -> ResetCoordinationResult:
    from services.coordination.sequencing import PostgresSequenceGate

    await PostgresSequenceGate().reset(req.task_id)
    return ResetCoordinationResult(sequence_reset=True)


@activity.defn
async def finalize_work_claim(req: FinalizeClaimRequest) -> FinalizeClaimResult:
    from services.coordination.claims import (
        COMPLETED,
        RELEASED,
        PostgresWorkClaimStore,
        claim_id_for,
    )

    claim_id = claim_id_for(req.task_id, req.work_unit)
    state = COMPLETED if req.completed else RELEASED
    if not req.owner_workflow_id:
        return FinalizeClaimResult(claim_id=claim_id, finalized=False, state=state)
    store = PostgresWorkClaimStore()
    if req.completed:
        finalized = await store.complete(claim_id, owner_workflow_id=req.owner_workflow_id)
    else:
        finalized = await store.release(claim_id, owner_workflow_id=req.owner_workflow_id)
    return FinalizeClaimResult(claim_id=claim_id, finalized=finalized, state=state)


@activity.defn
async def run_implementation_stage(req: ImplementationRequest) -> ImplementationResult:
    from scripts.config import get_settings

    from services.repository_intelligence.normalization.serialization import (
        render_context_summary,
    )

    context = render_context_summary(req.context_package) if req.context_package else req.context
    if req.plan:
        plan_block = f"Approved change plan:\n{req.plan}"
        context = f"{context}\n\n{plan_block}" if context else plan_block

    materializer = _build_materializer()
    workspace = await materializer.materialize(
        repo=req.repo,
        task_id=req.task_id,
        execution_id=req.execution_id,
        base=req.base,
        commit_sha=req.commit,
    )
    runtime = _build_runtime()
    outcome = await runtime.run_implementation(
        execution_id=req.execution_id,
        task=req.task,
        context=context,
        workspace_source=str(Path(workspace.path).resolve()),
        task_id=req.task_id,
    )
    staged = await materializer.stage(workspace)
    return ImplementationResult(
        status=outcome.status,
        terminal_reason=outcome.terminal_reason,
        tool_calls=outcome.tool_calls,
        elapsed_seconds=outcome.elapsed_seconds,
        cost=outcome.cost,
        tokens=outcome.tokens,
        llm_calls=outcome.llm_calls,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        cache_read_tokens=outcome.cache_read_tokens,
        cache_write_tokens=outcome.cache_write_tokens,
        branch=workspace.branch,
        commit_sha=workspace.commit_sha,
        diff_hash=staged.tree,
        model_id=get_settings().coding_model,
        prompt_version=IMPLEMENTATION_PROMPT_VERSION,
        workspace_path=workspace.path,
        changed_files=await materializer.changed_paths(workspace),
        summary=outcome.summary,
    )
