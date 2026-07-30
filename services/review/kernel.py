"""Review kernel seam: the pluggable finder-to-verdict backend behind the gate."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from services.review.types import (
    STAGE_TIERS,
    Finding,
    ModelProvenance,
    RawReview,
    ReviewInput,
    Verdict,
)


@runtime_checkable
class ReviewKernel(Protocol):
    def run(self, review: ReviewInput) -> RawReview: ...


class FakeReviewKernel:
    def __init__(self, raw: RawReview) -> None:
        self._raw = raw

    def run(self, review: ReviewInput) -> RawReview:
        return self._raw


def _finding_from_rebar(index: int, raw: dict[str, Any]) -> Finding:
    return Finding(
        finding_id=str(raw.get("id") or index),
        severity=str(raw.get("severity") or "unknown"),
        blocking=bool(raw.get("blocking", False)),
        title=str(raw.get("title") or raw.get("finding") or ""),
        detail=str(raw.get("detail") or raw.get("body") or raw.get("description") or ""),
        location=raw.get("location"),
    )


def _raw_from_runner_result(result: dict[str, Any], tier: str) -> RawReview:
    raw_findings = result.get("findings") or []
    findings = [_finding_from_rebar(i, f) for i, f in enumerate(raw_findings)]
    verdict = Verdict.BLOCK if any(f.blocking for f in findings) else Verdict.PASS
    provenance = [ModelProvenance(leg="finder", tier=tier, model_id=result.get("model"))]
    return RawReview(
        verdict=verdict,
        findings=findings,
        coaching=list(result.get("coaching") or []),
        provenance=provenance,
        summary=str(result.get("summary") or ""),
    )


class RebarReviewKernel:
    def run(self, review: ReviewInput) -> RawReview:
        from rebar.llm.gate_source import (
            annotate_result,
            apply_handle,
            gate_read_root,
            resolve_gate_handle,
        )
        from rebar.llm.runner import RunRequest, get_runner

        from services.review.llm import review_llm_config

        tier = STAGE_TIERS[review.stage]
        cfg = review_llm_config(tier)
        handle = resolve_gate_handle(review.ref, review.source, review.repo_root)
        cfg = apply_handle(cfg, handle)
        with gate_read_root(handle):
            result = get_runner(cfg).run(
                RunRequest(
                    system_prompt=review.system_prompt,
                    instructions=review.instructions,
                    config=cfg,
                    reviewers=list(review.reviewers),
                )
            )
        result = annotate_result(result, handle)
        return _raw_from_runner_result(result, tier)
