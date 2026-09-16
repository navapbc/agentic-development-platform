"""Temporal activities that run the platform review gates (plan, code, security)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from observability import bind_execution, token_cost_usd
from temporalio import activity

from services.prompts import load_prompt
from services.prompts.library import (
    ACCEPTANCE_VERIFICATION,
    CODE_REVIEW,
    PLAN_REVIEW,
    SECURITY_REVIEW,
)
from services.review.types import ReviewStage


@dataclass
class ReviewRequest:
    task_id: str
    execution_id: str
    subject: str
    context_ref: str = ""
    context_summary: str = ""
    repo: str = ""
    repo_root: str | None = None
    ref: str | None = None
    acceptance_criteria: str = ""


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
    cost_usd: float = 0.0


@dataclass(frozen=True)
class _PromptSpec:
    name: str
    subject_label: str


_PLAN = _PromptSpec(name="plan-review", subject_label="Change plan under review")
_CODE = _PromptSpec(name="code-review", subject_label="Code diff under review")
_SECURITY = _PromptSpec(
    name="security-review", subject_label="Code diff under security review"
)
_ACCEPTANCE = _PromptSpec(
    name="acceptance-verification",
    subject_label="Completion evidence under acceptance verification",
)

_PROMPT_VERSIONS_BY_GATE = {
    "plan-review": PLAN_REVIEW.version,
    "code-review": CODE_REVIEW.version,
    "security-review": SECURITY_REVIEW.version,
    "acceptance": ACCEPTANCE_VERIFICATION.version,
}


def review_prompt_versions(gate_names: Iterable[str]) -> dict[str, str]:
    return {
        name: _PROMPT_VERSIONS_BY_GATE[name]
        for name in gate_names
        if name in _PROMPT_VERSIONS_BY_GATE
    }



def build_review_gate(stage: ReviewStage | None = None):
    from services.review.evidence import build_evidence_sink
    from services.review.gate import ReviewGate, select_attestor
    from services.review.kernel import RebarReviewKernel

    attestor = select_attestor(stage) if stage is not None else None
    return ReviewGate(RebarReviewKernel(), build_evidence_sink(), attestor)


def _subject_ref(subject: str) -> str:
    return f"sha256:{hashlib.sha256(subject.encode('utf-8')).hexdigest()}"


def _instructions(stage: ReviewStage, spec: _PromptSpec, req: ReviewRequest) -> str:
    """Assemble the reviewer instructions for a stage.

    The acceptance finder is told its criteria are stated in the subject, so the
    criteria are appended for that stage. Every other stage judges the change itself
    and keeps the subject it already had.
    """
    parts = []
    if req.context_summary:
        parts.append(f"Repository context:\n{req.context_summary}")
    parts.append(f"{spec.subject_label}:\n{req.subject}")
    if stage is ReviewStage.ACCEPTANCE_VERIFICATION and req.acceptance_criteria.strip():
        parts.append(f"Acceptance criteria:\n{req.acceptance_criteria.strip()}")
    return "\n\n".join(parts)


_REPO_GROUNDED_STAGES = frozenset(
    {
        ReviewStage.CODE_REVIEW,
        ReviewStage.SECURITY_REVIEW,
        ReviewStage.ACCEPTANCE_VERIFICATION,
    }
)


def _resolve_repo_root(req: ReviewRequest) -> str | None:
    """Resolve a read-only repository root for the reviewer, reusing the RIS checkout.

    Prefers an explicit root on the request. Otherwise maps the repository name to
    its configured repository-intelligence path, so the reviewer reads the same
    read-only checkout the intelligence service indexes rather than a second copy.
    Returns ``None`` when no path resolves or the resolved path is not present, so
    the reviewer falls back to the context summary rather than a missing directory.
    """
    candidate = req.repo_root
    if not candidate and req.repo:
        from scripts.config import get_settings

        entry = get_settings().repo_intelligence_registry().get(req.repo)
        candidate = entry[0] if entry else None
    if candidate and Path(candidate).is_dir():
        return candidate
    return None


async def _run_gate(stage: ReviewStage, spec: _PromptSpec, req: ReviewRequest) -> ReviewOutcome:
    from services.review.types import ReviewInput

    loaded = load_prompt(spec.name)
    gate = build_review_gate(stage)
    repo_root = _resolve_repo_root(req) if stage in _REPO_GROUNDED_STAGES else None
    review_input = ReviewInput(
        stage=stage,
        subject_ref=_subject_ref(req.subject),
        context_ref=req.context_ref,
        prompt_name=spec.name,
        prompt_version=loaded.version,
        system_prompt=loaded.text,
        instructions=_instructions(stage, spec, req),
        repo_root=repo_root,
        ref=req.ref,
        source="local",
    )
    with bind_execution(stage=stage.value, prompt_version=loaded.version):
        result = await gate.review(review_input, execution_id=req.execution_id)
    model_id = result.provenance[0].model_id if result.provenance else None
    usage = result.usage
    cost_usd = token_cost_usd(
        model_id,
        usage.prompt_tokens,
        usage.completion_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
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
        cost_usd=cost_usd,
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
