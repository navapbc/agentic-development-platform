"""Normalized input and output types for the platform review gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ReviewStage(str, Enum):
    PLAN_REVIEW = "plan-review"
    CODE_REVIEW = "code-review"
    SECURITY_REVIEW = "security-review"
    ACCEPTANCE_VERIFICATION = "acceptance-verification"


class Verdict(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    INDETERMINATE = "indeterminate"


STAGE_TIERS: dict[ReviewStage, str] = {
    ReviewStage.PLAN_REVIEW: "review-model",
    ReviewStage.CODE_REVIEW: "review-model",
    ReviewStage.SECURITY_REVIEW: "security-review-model",
    ReviewStage.ACCEPTANCE_VERIFICATION: "evaluation-model",
}


@dataclass(frozen=True)
class ReviewInput:
    stage: ReviewStage
    subject_ref: str
    context_ref: str
    prompt_name: str
    prompt_version: str
    system_prompt: str
    instructions: str
    reviewers: tuple[str, ...] = ()
    repo_root: str | None = None
    ref: str | None = None
    source: str = "local"


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: str
    blocking: bool
    title: str
    detail: str
    location: str | None = None


@dataclass(frozen=True)
class ModelProvenance:
    leg: str
    tier: str
    model_id: str | None


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_calls: int = 0


@dataclass(frozen=True)
class Attestation:
    algorithm: str
    payload_version: int
    content_hash: str
    signature: str
    key_id: str
    issued_at: str


@dataclass
class RawReview:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    coaching: list[str] = field(default_factory=list)
    provenance: list[ModelProvenance] = field(default_factory=list)
    summary: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class ReviewResult:
    stage: ReviewStage
    subject_ref: str
    prompt_name: str
    prompt_version: str
    verdict: Verdict
    findings: list[Finding]
    coaching: list[str]
    provenance: list[ModelProvenance]
    summary: str
    attestation: Attestation
    usage: TokenUsage = field(default_factory=TokenUsage)
