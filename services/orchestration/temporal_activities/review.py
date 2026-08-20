"""Temporal activities that run the platform review gates (plan, code, security)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from temporalio import activity

from services.review.types import ReviewStage


@dataclass
class ReviewRequest:
    task_id: str
    execution_id: str
    subject: str
    context_ref: str = ""
    context_summary: str = ""
    repo_root: str | None = None
    ref: str | None = None


@dataclass
class ReviewOutcome:
    verdict: str
    attestation_hash: str
    prompt_version: str
    model_id: str | None = None
    summary: str = ""
    blocking_findings: int = 0
    findings: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0


@dataclass(frozen=True)
class _PromptSpec:
    name: str
    version: str
    system_prompt: str
    subject_label: str


_PLAN = _PromptSpec(
    name="plan-review",
    version="plan-review/v1",
    system_prompt=(
        "You are a rigorous software change-plan reviewer. Assess the plan for correctness, "
        "safety, scope discipline, and testability. Raise blocking findings only for defects "
        "that would make the plan unsafe or incorrect to implement."
    ),
    subject_label="Change plan under review",
)
_CODE = _PromptSpec(
    name="code-review",
    version="code-review/v1",
    system_prompt=(
        "You are a rigorous code reviewer. Assess the diff for correctness, regressions, and "
        "adherence to the plan. Raise blocking findings only for defects that would make the "
        "change unsafe or incorrect to merge."
    ),
    subject_label="Code diff under review",
)
_SECURITY = _PromptSpec(
    name="security-review",
    version="security-review/v1",
    system_prompt=(
        "You are a rigorous application security reviewer. Assess the diff for injection, "
        "authorization, secret-handling, and other OWASP-class weaknesses. Raise blocking "
        "findings only for exploitable vulnerabilities the change introduces."
    ),
    subject_label="Code diff under security review",
)
_ACCEPTANCE = _PromptSpec(
    name="acceptance-verification",
    version="acceptance-verification/v1",
    system_prompt=(
        "You are a rigorous completion verifier. Judge whether the implemented change satisfies "
        "the task's acceptance criteria and whether the supporting evidence substantiates the "
        "completion claim. Raise blocking findings only for unmet criteria or unsupported claims."
    ),
    subject_label="Completion evidence under acceptance verification",
)

_PROMPT_VERSIONS_BY_GATE = {
    "plan-review": _PLAN.version,
    "code-review": _CODE.version,
    "security-review": _SECURITY.version,
    "acceptance": _ACCEPTANCE.version,
}


def review_prompt_versions(gate_names: Iterable[str]) -> dict[str, str]:
    return {
        name: _PROMPT_VERSIONS_BY_GATE[name]
        for name in gate_names
        if name in _PROMPT_VERSIONS_BY_GATE
    }



def build_review_gate():
    from services.review.evidence import PostgresEvidenceSink
    from services.review.gate import ReviewGate
    from services.review.kernel import RebarReviewKernel

    return ReviewGate(RebarReviewKernel(), PostgresEvidenceSink())


def _subject_ref(subject: str) -> str:
    return f"sha256:{hashlib.sha256(subject.encode('utf-8')).hexdigest()}"


def _instructions(spec: _PromptSpec, req: ReviewRequest) -> str:
    parts = []
    if req.context_summary:
        parts.append(f"Repository context:\n{req.context_summary}")
    parts.append(f"{spec.subject_label}:\n{req.subject}")
    return "\n\n".join(parts)


async def _run_gate(stage: ReviewStage, spec: _PromptSpec, req: ReviewRequest) -> ReviewOutcome:
    from services.review.types import ReviewInput

    gate = build_review_gate()
    review_input = ReviewInput(
        stage=stage,
        subject_ref=_subject_ref(req.subject),
        context_ref=req.context_ref,
        prompt_name=spec.name,
        prompt_version=spec.version,
        system_prompt=spec.system_prompt,
        instructions=_instructions(spec, req),
        repo_root=req.repo_root,
        ref=req.ref,
        source="local",
    )
    result = await gate.review(review_input, execution_id=req.execution_id)
    model_id = result.provenance[0].model_id if result.provenance else None
    usage = result.usage
    return ReviewOutcome(
        verdict=result.verdict.value,
        attestation_hash=result.attestation.content_hash,
        prompt_version=result.prompt_version,
        model_id=model_id,
        summary=result.summary,
        blocking_findings=sum(1 for f in result.findings if f.blocking),
        findings=[f.title for f in result.findings if f.blocking],
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        total_tokens=usage.prompt_tokens + usage.completion_tokens,
        llm_calls=usage.llm_calls,
    )


@activity.defn
async def review_plan(req: ReviewRequest) -> ReviewOutcome:
    return await _run_gate(ReviewStage.PLAN_REVIEW, _PLAN, req)


@activity.defn
async def review_code(req: ReviewRequest) -> ReviewOutcome:
    return await _run_gate(ReviewStage.CODE_REVIEW, _CODE, req)


@activity.defn
async def review_security(req: ReviewRequest) -> ReviewOutcome:
    return await _run_gate(ReviewStage.SECURITY_REVIEW, _SECURITY, req)


@activity.defn
async def verify_acceptance(req: ReviewRequest) -> ReviewOutcome:
    return await _run_gate(ReviewStage.ACCEPTANCE_VERIFICATION, _ACCEPTANCE, req)
