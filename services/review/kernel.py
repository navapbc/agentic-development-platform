"""Review kernel seam: the pluggable finder-to-verdict backend behind the gate."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from services.review.types import (
    STAGE_TIERS,
    Finding,
    ModelProvenance,
    RawReview,
    ReviewInput,
    ReviewStage,
    Verdict,
)

_MULTIPASS: dict[ReviewStage, dict[str, str]] = {
    ReviewStage.PLAN_REVIEW: {
        "subject": "plan",
        "criteria": "correctness, safety, scope, testability",
        "finder_schema": "plan_review_findings",
        "verify_schema": "plan_review_verification",
    },
    ReviewStage.CODE_REVIEW: {
        "subject": "change",
        "criteria": "correctness, regressions, test coverage, adherence to the plan",
        "finder_schema": "code_review_findings",
        "verify_schema": "code_review_verification",
    },
    ReviewStage.SECURITY_REVIEW: {
        "subject": "change",
        "criteria": "injection, authorization, secret handling, other OWASP-class weaknesses",
        "finder_schema": "code_review_findings",
        "verify_schema": "code_review_verification",
    },
    ReviewStage.ACCEPTANCE_VERIFICATION: {
        "subject": "completion",
        "criteria": "unmet acceptance criteria, unsupported completion claims",
        "finder_schema": "code_review_findings",
        "verify_schema": "code_review_verification",
    },
}


@runtime_checkable
class ReviewKernel(Protocol):
    def run(self, review: ReviewInput) -> RawReview: ...


class FakeReviewKernel:
    def __init__(self, raw: RawReview) -> None:
        self._raw = raw

    def run(self, review: ReviewInput) -> RawReview:
        return self._raw


def _register_and_impact(finder_schema: str) -> Any:
    """Register the pass contracts for a review family and return its impact fn."""
    if finder_schema.startswith("plan_review"):
        import rebar.llm.plan_review.passes  # noqa: F401  registers the plan contracts
        from rebar.llm.review_kernel import impact_plan

        return impact_plan
    import rebar.llm.code_review  # noqa: F401  registers the code contracts
    from rebar.llm.review_kernel import impact_code

    return impact_code


def _raw_from_decided(
    decided: list[dict[str, Any]], tier: str, model_id: str | None
) -> RawReview:
    """Map the kernel's pass-3 decisions onto a RawReview.

    ``block`` decisions are the only blocking findings. ``advisory`` and
    ``indeterminate`` are recorded as non-blocking so an unverified finding is
    surfaced rather than silently treated as clean. ``dropped`` (a veto or low
    validity) is discarded.
    """
    findings: list[Finding] = []
    for index, item in enumerate(decided):
        decision = str(item.get("decision") or "")
        if decision == "dropped":
            continue
        claim = str(item.get("finding") or "").strip()
        if not claim:
            continue
        findings.append(
            Finding(
                finding_id=str(index),
                severity=str(item.get("severity") or "none"),
                blocking=decision == "block",
                title=claim,
                detail=f"[{decision}] {item.get('reason', '')}".strip(),
                location=None,
            )
        )
    verdict = Verdict.BLOCK if any(f.blocking for f in findings) else Verdict.PASS
    provenance = [ModelProvenance(leg="finder", tier=tier, model_id=model_id)]
    return RawReview(verdict=verdict, findings=findings, provenance=provenance)


class RebarReviewKernel:
    def run(self, review: ReviewInput) -> RawReview:
        from dataclasses import replace

        from rebar.llm.gate_source import (
            apply_handle,
            gate_read_root,
            resolve_gate_handle,
        )

        from services.review.llm import review_llm_config

        tier = STAGE_TIERS[review.stage]
        cfg = review_llm_config(tier)
        if review.repo_root:
            cfg = replace(cfg, repo_path=review.repo_root)
        handle = resolve_gate_handle(review.ref, review.source, review.repo_root)
        cfg = apply_handle(cfg, handle)
        with gate_read_root(handle):
            return self._run_multipass(review, cfg, tier)

    def _run_multipass(self, review: ReviewInput, cfg: Any, tier: str) -> RawReview:
        from dataclasses import replace

        from rebar.llm.review_kernel import (
            VERIFIER_RULES_SCAFFOLD,
            pass3_over_findings,
            suppress_and_dedup,
            verify_findings,
        )
        from rebar.llm.runner import RunRequest, get_runner

        spec = _MULTIPASS[review.stage]
        impact_fn = _register_and_impact(spec["finder_schema"])
        runner = get_runner(cfg)
        cfg = replace(cfg, temperature=0.0)
        exec_mode = "agentic" if review.repo_root else "single_turn"
        reviewers = list(review.reviewers) or ["plan-reviewer"]
        subject = spec["subject"]

        finder = RunRequest(
            system_prompt=review.system_prompt,
            instructions=(
                f"## The {subject} under review\n{review.instructions}\n\n"
                f"Surface every grounded defect in this {subject}. Tag each finding with one "
                f"or more criteria from {spec['criteria']}. Provide the evidence that grounds "
                "it, a quote from the subject or a repository citation, and its impact. Return "
                f"an empty list for a sound {subject}."
            ),
            config=cfg,
            reviewers=reviewers,
            mode="structured",
            output_schema=spec["finder_schema"],
            execution_mode=exec_mode,
        )
        pass1 = runner.run(finder).get("findings", []) or []
        kept, _dropped = suppress_and_dedup(pass1)

        def run_chunk(instructions: str, context: str) -> list[dict[str, Any]]:
            request = RunRequest(
                system_prompt=VERIFIER_RULES_SCAFFOLD,
                instructions=f"{instructions}\n\n## The {subject} under review\n{context}",
                config=cfg,
                reviewers=reviewers,
                mode="structured",
                output_schema=spec["verify_schema"],
                execution_mode=exec_mode,
            )
            return runner.run(request).get("verifications", []) or []

        verified = verify_findings(
            kept,
            context=review.instructions,
            run_chunk=run_chunk,
            window_tokens=100_000,
            est_tokens=lambda text: len(text) // 4 + 1,
        )
        decided = pass3_over_findings(
            kept,
            verified["verifications"],
            threshold_for=lambda _criteria: (0.95, True),
            impact_fn=impact_fn,
        )
        model_id = getattr(cfg, "model", None)
        return _raw_from_decided(decided, tier, model_id)
