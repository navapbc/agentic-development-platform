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
    TokenUsage,
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


def _finder_instructions(stage: ReviewStage, subject: str, criteria: str, review_body: str) -> str:
    """Build the finder-pass instructions for a stage.

    The acceptance stage judges completion-criteria coverage rather than code
    defects, so it drives the finder criterion by criterion and blocks on any
    criterion the change does not fully satisfy and evidence. Every other stage
    surfaces grounded defects tagged with the stage's criteria.
    """
    header = f"## The {subject} under review\n{review_body}\n\n"
    if stage is ReviewStage.ACCEPTANCE_VERIFICATION:
        return header + (
            "The acceptance criteria are stated in the subject above. Evaluate the change "
            "against each acceptance criterion in turn. Emit one blocking finding for every "
            "criterion the change does not fully satisfy, satisfies only partially, or that the "
            "summary claims is complete without evidence in the change. Name the criterion, quote "
            "the criterion and the change or its absence that grounds the gap, and state why the "
            "criterion is not met. Judge only whether the change meets the stated acceptance "
            "criteria, not code style or defects unrelated to the criteria. Return an empty list "
            "only when every acceptance criterion is fully satisfied and evidenced by the change."
        )
    return header + (
        f"Surface every grounded defect in this {subject}. Tag each finding with "
        f"one or more criteria from {criteria}. Provide the evidence that "
        "grounds it, a quote from the subject or a repository citation, and its "
        f"impact. Return an empty list for a sound {subject}."
    )


CODE_BLOCK_THRESHOLD = 0.95
ACCEPTANCE_BLOCK_THRESHOLD = 0.5


def _decision_params(stage: ReviewStage, impact_fn: Any) -> tuple[Any, Any]:
    """Select the pass-3 impact model and block threshold for a stage.

    Acceptance coverage is binary rather than severity-graded. A code-defect impact
    model scores an unmet-criterion finding too low to block, so the acceptance stage
    scores every finding at full impact and blocks on the verification's validity
    instead, meaning a verifier-confirmed unmet criterion blocks the completion. Every
    other stage keeps its code or plan impact model and the high code threshold.
    """
    if stage is ReviewStage.ACCEPTANCE_VERIFICATION:
        return (lambda _attrs: 1.0), (lambda _criteria: (ACCEPTANCE_BLOCK_THRESHOLD, True))
    return impact_fn, (lambda _criteria: (CODE_BLOCK_THRESHOLD, True))




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


def _accumulate_usage(acc: dict[str, int], result: dict[str, Any]) -> None:
    """Add a runner result's per-call token usage into the accumulator."""
    usage = result.get("_usage") or {}
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "requests",
    )
    for key in keys:
        acc[key] = acc.get(key, 0) + int(usage.get(key, 0) or 0)


def _usage_from_acc(acc: dict[str, int]) -> TokenUsage:
    """Map the accumulated runner usage onto the review TokenUsage fields."""
    return TokenUsage(
        prompt_tokens=acc.get("input_tokens", 0),
        completion_tokens=acc.get("output_tokens", 0),
        cache_read_tokens=acc.get("cache_read_tokens", 0),
        cache_write_tokens=acc.get("cache_write_tokens", 0),
        llm_calls=acc.get("requests", 0),
    )


def _is_failover_exhaustion(exc: BaseException) -> bool:
    """True when ``exc`` is, or wraps, an all-legs-failed FallbackModel error.

    The runner can surface the group directly or nested inside chained or grouped
    exceptions, so the cause, context, and any ExceptionGroup members are walked.
    """
    from pydantic_ai.exceptions import FallbackExceptionGroup

    seen: set[int] = set()

    def _walk(node: BaseException | None) -> bool:
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        if isinstance(node, FallbackExceptionGroup):
            return True
        if isinstance(node, BaseExceptionGroup) and any(_walk(sub) for sub in node.exceptions):
            return True
        return _walk(node.__cause__) or _walk(node.__context__)

    return _walk(exc)


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
        try:
            with gate_read_root(handle):
                return self._run_multipass(review, cfg, tier)
        except BaseException as exc:  # noqa: BLE001  narrowed by _is_failover_exhaustion
            if not _is_failover_exhaustion(exc):
                raise
            return self._block_on_failover(cfg, tier)

    def _block_on_failover(self, cfg: Any, tier: str) -> RawReview:
        """Return a blocking verdict when every native review leg was unavailable.

        A both-leg failure follows the normal block path with a zero-usage row so
        the gate attests and records it rather than raising into the workflow.
        """
        finding = Finding(
            finding_id="0",
            severity="high",
            blocking=True,
            title="Review model unavailable",
            detail="Every review model leg failed to respond, so the change cannot be cleared.",
            location=None,
        )
        provenance = [
            ModelProvenance(leg="finder", tier=tier, model_id=self._provenance_model_id(cfg, tier))
        ]
        return RawReview(verdict=Verdict.BLOCK, findings=[finding], provenance=provenance)

    def _run_multipass(self, review: ReviewInput, cfg: Any, tier: str) -> RawReview:
        from dataclasses import replace

        from rebar.llm.review_kernel import (
            VERIFIER_RULES_SCAFFOLD,
            pass3_over_findings,
            suppress_and_dedup,
            verify_findings,
        )
        from rebar.llm.runner import RunRequest

        spec = _MULTIPASS[review.stage]
        impact_fn = _register_and_impact(spec["finder_schema"])
        cfg = replace(cfg, temperature=0.0)
        reviewers = list(review.reviewers) or ["plan-reviewer"]
        subject = spec["subject"]
        usage_acc: dict[str, int] = {}
        runner = self._runner_for(cfg)
        answered_model: str | None = None
        exec_mode = "agentic" if review.repo_root else "single_turn"

        finder = RunRequest(
            system_prompt=review.system_prompt,
            instructions=_finder_instructions(
                review.stage, subject, spec["criteria"], review.instructions
            ),
            config=cfg,
            reviewers=reviewers,
            mode="structured",
            output_schema=spec["finder_schema"],
            execution_mode=exec_mode,
        )
        finder_result = runner.run(finder)
        _accumulate_usage(usage_acc, finder_result)
        answered_model = finder_result.get("model") or answered_model
        pass1 = finder_result.get("findings", []) or []
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
            chunk_result = runner.run(request)
            _accumulate_usage(usage_acc, chunk_result)
            return chunk_result.get("verifications", []) or []

        verified = verify_findings(
            kept,
            context=review.instructions,
            run_chunk=run_chunk,
            window_tokens=100_000,
            est_tokens=lambda text: len(text) // 4 + 1,
        )
        decide_impact_fn, threshold_for = _decision_params(review.stage, impact_fn)
        decided = pass3_over_findings(
            kept,
            verified["verifications"],
            threshold_for=threshold_for,
            impact_fn=decide_impact_fn,
        )
        model_id = answered_model or self._provenance_model_id(cfg, tier)
        raw = _raw_from_decided(decided, tier, model_id)
        raw.usage = _usage_from_acc(usage_acc)
        return raw

    @staticmethod
    def _runner_for(cfg: Any) -> Any:
        """Build the rebar runner for a review config.

        Native review runs through rebar's normal runner path, not the model-override
        hook: the runner builds a real Bedrock model object (carrying a
        ``BedrockModelProfile``), which is what enables Anthropic prompt caching, and
        it reads the Claude-to-Nova fallback chain from the model-class config so the
        failover is native. The model-override hook forces caching off, so it is not
        used here.
        """
        from rebar.llm.runner import get_runner

        return get_runner(cfg)

    @staticmethod
    def _provenance_model_id(cfg: Any, tier: str) -> str | None:
        return getattr(cfg, "model", None)
