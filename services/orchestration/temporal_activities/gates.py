"""Temporal activity that resolves a gate by name and evaluates it."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from observability import record_gate_outcome, stage_span
from temporalio import activity

from services.orchestration.gates.contract import (
    BLOCK,
    REVIEW,
    SCANNER,
    GateContext,
    GateFinding,
    GateNode,
    GateResult,
)


@dataclass
class EvaluateGateRequest:
    gate: str
    context: GateContext


@dataclass
class GatePlan:
    review: list[GateNode] = field(default_factory=list)
    scanner: list[GateNode] = field(default_factory=list)
    fail_fast_review: bool = False
    fail_fast_scanner: bool = False
    plan_pause: bool = False


@dataclass
class ClassifyTestFailuresRequest:
    task_id: str
    execution_id: str
    repo: str
    ref: str
    repo_root: str


@dataclass
class TriageResult:
    enabled: bool
    action: str = ""
    should_patch: bool = False
    flaky_only: bool = False
    has_environment_issue: bool = False
    summary: str = ""


@activity.defn
async def classify_test_failures(req: ClassifyTestFailuresRequest) -> TriageResult:
    from adapters.interfaces.security_scanner import ScanTarget
    from scripts.config import get_settings

    from services.orchestration.gates.engine_source import (
        load_gate_engine_configs,
        triage_engine,
    )
    from services.orchestration.gates.gate_engine import AiTestClassifier, triage_action

    settings = get_settings()
    config = triage_engine(load_gate_engine_configs(settings))
    if config is None:
        return TriageResult(enabled=False)

    classifier = AiTestClassifier(
        name="test-triage",
        config=config,
        litellm_base_url=settings.litellm_base_url,
        litellm_api_key=settings.litellm_master_key,
    )
    triage = await classifier.classify(
        ScanTarget(repo=req.repo, ref=req.ref, repo_root=req.repo_root)
    )
    return TriageResult(
        enabled=True,
        action=triage_action(triage),
        should_patch=triage.should_patch,
        flaky_only=triage.flaky_only,
        has_environment_issue=triage.has_environment_issue,
        summary=triage.summary,
    )


def _gate_content_hash(execution_id: str, result: GateResult) -> str:
    payload = json.dumps(
        {
            "execution_id": execution_id,
            "name": result.name,
            "verdict": result.verdict,
            "findings": [
                {"severity": finding.severity, "title": finding.title, "rule_id": finding.rule_id}
                for finding in result.findings
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


async def _persist_gate_evidence(context: GateContext, result: GateResult) -> None:
    from services.review.evidence import EvidenceRecord, PostgresEvidenceSink

    content_hash = result.attestation_hash or _gate_content_hash(context.execution_id, result)
    record = EvidenceRecord(
        artifact_id=uuid.uuid4().hex,
        execution_id=context.execution_id,
        content_hash=content_hash,
        source_component=f"gate:{result.name}",
        classification="final",
        metadata={
            "gate": result.name,
            "kind": result.kind,
            "verdict": result.verdict,
            "finding_count": len(result.findings),
            "attestation_hash": result.attestation_hash,
        },
    )
    await PostgresEvidenceSink().persist(record)


def _backend_error_result(name: str, kind: str, exc: Exception) -> GateResult:
    """A gate whose backend is absent or broken fails closed as a deterministic block.

    A required gate then blocks promotion and honors its on_block response the same as any
    block verdict, rather than crashing the workflow, so a misconfigured or unavailable
    external tool never silently skips and never takes the run down.
    """
    reason = f"{type(exc).__name__}: {exc}".strip()
    return GateResult(
        name=name,
        kind=kind or SCANNER,
        verdict=BLOCK,
        findings=[
            GateFinding(
                severity="high",
                title=f"gate {name!r} backend failed",
                detail=reason[:800],
                rule_id="gate-backend-error",
            )
        ],
        summary=f"gate backend error: {reason[:200]}",
        metadata={"backend_error": True},
    )


@activity.defn
async def evaluate_gate(req: EvaluateGateRequest) -> GateResult:
    from services.orchestration.gates.config import load_gates_config
    from services.orchestration.gates.registry import registry_for_config

    registry = registry_for_config(load_gates_config())
    gate = registry.resolve(req.gate)
    with stage_span(
        f"gate.{req.gate}",
        stage=gate.kind or REVIEW,
        gate=req.gate,
        execution_id=req.context.execution_id,
        task_id=req.context.task_id,
    ):
        try:
            result = await gate.evaluate(req.context)
        except Exception as exc:  # noqa: BLE001 - a backend failure fails closed, never crashes
            result = _backend_error_result(req.gate, gate.kind, exc)
        await _persist_gate_evidence(req.context, result)
    record_gate_outcome(result.name, result.verdict)
    return result



@activity.defn
async def load_gate_plan() -> GatePlan:
    from services.orchestration.gates.config import (
        HUMAN_GATES,
        PLAN_GATE,
        load_gates_config,
    )
    from services.orchestration.gates.registry import registry_for_config

    config = load_gates_config()
    registry = registry_for_config(config)
    review: list[GateNode] = []
    scanner: list[GateNode] = []
    plan_pause = False
    for spec in config.enabled_gates():
        if spec.name in HUMAN_GATES:
            if spec.name == PLAN_GATE:
                plan_pause = True
            continue
        node = GateNode(
            name=spec.name,
            required=spec.required,
            after=spec.after,
            on_block=spec.on_block,
        )
        kind = registry.resolve(spec.name).kind
        if kind == REVIEW:
            review.append(node)
        elif kind == SCANNER:
            scanner.append(node)
    return GatePlan(
        review=review,
        scanner=scanner,
        fail_fast_review=config.is_fail_fast("review"),
        fail_fast_scanner=config.is_fail_fast("scanner"),
        plan_pause=plan_pause,
    )

