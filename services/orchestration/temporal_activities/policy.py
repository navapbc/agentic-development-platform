"""Temporal activity that evaluates the promotion policy for a change."""

from __future__ import annotations

from dataclasses import dataclass, field

from adapters.interfaces.policy_engine import PolicyGate
from temporalio import activity


@dataclass
class EvaluatePromotionRequest:
    task_id: str
    execution_id: str
    jira_project: str
    repository: str
    ci_status: str
    repair_attempts: int
    max_repair_attempts: int
    jira_snapshot_current: bool
    pr_authorized: bool
    gates: list[PolicyGate] = field(default_factory=list)
    required_gates: list[str] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    scan_findings: list[dict[str, str]] = field(default_factory=list)


@dataclass
class EvaluatePromotionResult:
    allow: bool
    bundle_version: str
    next_action: str
    reasons: list[str] = field(default_factory=list)
    human_review: list[str] = field(default_factory=list)


def _build_policy_mediator():
    from adapters.real.policy_engine import RealOpaClient

    from services.integration.opa.mediator import PromotionPolicyMediator

    return PromotionPolicyMediator.from_settings(RealOpaClient.from_settings())


@activity.defn
async def evaluate_promotion(req: EvaluatePromotionRequest) -> EvaluatePromotionResult:
    from adapters.interfaces.policy_engine import PolicyInput

    mediator = _build_policy_mediator()
    decision = await mediator.evaluate(
        PolicyInput(
            jira_project=req.jira_project,
            repository=req.repository,
            ci_status=req.ci_status,
            repair_attempts=req.repair_attempts,
            max_repair_attempts=req.max_repair_attempts,
            jira_snapshot_current=req.jira_snapshot_current,
            pr_authorized=req.pr_authorized,
            gates=list(req.gates),
            required_gates=list(req.required_gates),
            severity_counts=dict(req.severity_counts),
            scan_findings=list(req.scan_findings),
        )
    )
    return EvaluatePromotionResult(
        allow=decision.allow,
        bundle_version=decision.bundle_version,
        next_action=decision.next_action,
        reasons=list(decision.reasons),
        human_review=list(decision.human_review),
    )
