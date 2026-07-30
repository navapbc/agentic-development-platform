"""Temporal activity that runs the platform plan-review gate."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from temporalio import activity

PROMPT_NAME = "plan-review"
PROMPT_VERSION = "plan-review/v1"
SYSTEM_PROMPT = (
    "You are a rigorous software change-plan reviewer. Assess the plan for correctness, "
    "safety, scope discipline, and testability. Raise blocking findings only for defects "
    "that would make the plan unsafe or incorrect to implement."
)


@dataclass
class PlanReviewRequest:
    task_id: str
    execution_id: str
    plan: str
    context_ref: str = ""
    context_summary: str = ""
    repo_root: str | None = None
    ref: str | None = None


@dataclass
class PlanReviewOutcome:
    verdict: str
    attestation_hash: str
    prompt_version: str
    model_id: str | None = None
    summary: str = ""
    blocking_findings: int = 0
    findings: list[str] = field(default_factory=list)


def _subject_ref(plan: str) -> str:
    return f"sha256:{hashlib.sha256(plan.encode('utf-8')).hexdigest()}"


def _instructions(req: PlanReviewRequest) -> str:
    parts = []
    if req.context_summary:
        parts.append(f"Repository context:\n{req.context_summary}")
    parts.append(f"Change plan under review:\n{req.plan}")
    return "\n\n".join(parts)


@activity.defn
async def review_plan(req: PlanReviewRequest) -> PlanReviewOutcome:
    from services.review.evidence import PostgresEvidenceSink
    from services.review.gate import ReviewGate
    from services.review.kernel import RebarReviewKernel
    from services.review.types import ReviewInput, ReviewStage

    gate = ReviewGate(RebarReviewKernel(), PostgresEvidenceSink())
    review_input = ReviewInput(
        stage=ReviewStage.PLAN_REVIEW,
        subject_ref=_subject_ref(req.plan),
        context_ref=req.context_ref,
        prompt_name=PROMPT_NAME,
        prompt_version=PROMPT_VERSION,
        system_prompt=SYSTEM_PROMPT,
        instructions=_instructions(req),
        repo_root=req.repo_root,
        ref=req.ref,
        source="local",
    )
    result = await gate.review(review_input, execution_id=req.execution_id)
    model_id = result.provenance[0].model_id if result.provenance else None
    return PlanReviewOutcome(
        verdict=result.verdict.value,
        attestation_hash=result.attestation.content_hash,
        prompt_version=result.prompt_version,
        model_id=model_id,
        summary=result.summary,
        blocking_findings=sum(1 for f in result.findings if f.blocking),
        findings=[f.title for f in result.findings],
    )
