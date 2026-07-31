"""Temporal activities for the IMPLEMENTING execution stage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity

IMPLEMENTATION_PROMPT_VERSION = "implementation/v1"


@dataclass
class ClaimRequest:
    task_id: str
    execution_id: str
    work_unit: str
    agent_id: str
    file_impact: list[str] = field(default_factory=list)


@dataclass
class ClaimResult:
    claim_id: str
    won: bool
    claim_version: int


@dataclass
class ImplementationRequest:
    task_id: str
    execution_id: str
    repo: str
    base: str
    commit: str
    task: str
    context: str = ""


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
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""


def _hash_changes(changes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(changes):
        content_hash = hashlib.sha256(changes[path].encode("utf-8")).hexdigest()
        digest.update(f"{path}\x1f{content_hash}\x1e".encode())
    return f"sha256:{digest.hexdigest()}"


def _build_coordination_client():
    from services.orchestration.coordination_client import CoordinationClient

    return CoordinationClient()


def _build_materializer():
    from services.coordination.orphans import PostgresOrphanRegistry
    from services.orchestration.materialization import WorkspaceMaterializer

    return WorkspaceMaterializer.from_settings(PostgresOrphanRegistry())


def _build_runtime():
    from openhands.runtime.conversation import RemoteConversationDriver
    from openhands.runtime.provisioner import KubernetesSandboxProvisioner
    from openhands.runtime.runtime import SandboxRuntime
    from scripts.config import get_settings

    settings = get_settings()
    driver = RemoteConversationDriver(
        model=settings.coding_model,
        litellm_base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
    )
    return SandboxRuntime.from_settings(KubernetesSandboxProvisioner(), driver)


@activity.defn
async def claim_work_unit(req: ClaimRequest) -> ClaimResult:
    from services.coordination.claims import WorkUnit

    client = _build_coordination_client()
    outcome = await client.try_claim(
        WorkUnit(
            task_id=req.task_id,
            work_unit=req.work_unit,
            execution_id=req.execution_id,
            agent_id=req.agent_id,
            file_impact=tuple(req.file_impact),
        )
    )
    return ClaimResult(
        claim_id=outcome.claim_id,
        won=outcome.won,
        claim_version=outcome.claim_version,
    )


@activity.defn
async def run_implementation_stage(req: ImplementationRequest) -> ImplementationResult:
    from scripts.config import get_settings

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
        context=req.context,
        workspace_sub_path=str(Path(workspace.path).relative_to(materializer.workspace_root)),
    )
    changes = await materializer.collect_changes(workspace)
    return ImplementationResult(
        status=outcome.status,
        terminal_reason=outcome.terminal_reason,
        tool_calls=outcome.tool_calls,
        elapsed_seconds=outcome.elapsed_seconds,
        cost=outcome.cost,
        tokens=outcome.tokens,
        llm_calls=outcome.llm_calls,
        branch=workspace.branch,
        commit_sha=workspace.commit_sha,
        diff_hash=_hash_changes(changes),
        model_id=get_settings().coding_model,
        prompt_version=IMPLEMENTATION_PROMPT_VERSION,
        changed_files=sorted(changes),
        summary=outcome.summary,
    )
