"""Temporal activity that emits the determinism manifest, the first audit event of a run."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class RecordManifestRequest:
    execution_id: str
    task_id: str
    repository_sha: str = ""
    snapshot_version: int = 0


async def _resolve_bundle_version() -> str:
    from adapters.real.policy_engine import RealOpaClient

    return await RealOpaClient.from_settings().bundle_version()


async def _resolve_models() -> dict[str, str]:
    from scripts.config import get_settings

    from services.orchestration.temporal_activities.bedrock import resolve_logical_models
    from services.orchestration.temporal_workflows.review import PLANNING_MODEL

    settings = get_settings()
    return await resolve_logical_models([PLANNING_MODEL, settings.coding_model])


@activity.defn
async def record_manifest(req: RecordManifestRequest) -> str:
    from services.coordination.manifest import DeterminismManifest, PostgresManifestStore
    from services.orchestration.gates.config import load_gates_config
    from services.orchestration.gates.manifest import gate_manifest
    from services.orchestration.temporal_activities.review import review_prompt_versions

    config = load_gates_config()
    gates = gate_manifest(config)
    prompt_versions = review_prompt_versions(spec.name for spec in config.enabled_gates())
    bundle_version = await _resolve_bundle_version()
    resolved_models = await _resolve_models()

    manifest = DeterminismManifest(
        execution_id=req.execution_id,
        task_id=req.task_id,
        snapshot_version=req.snapshot_version,
        repository_sha=req.repository_sha,
        policy_bundle_version=bundle_version,
        prompt_versions=prompt_versions,
        resolved_models=resolved_models,
        gate_versions=gates.versions,
        gate_config_hash=gates.config_hash,
    )
    record = await PostgresManifestStore().record(manifest)
    return record.event_id
