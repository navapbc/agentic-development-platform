"""Unit tests for the rebar review-kernel runner seam."""

from __future__ import annotations

import contextlib
import types

import pytest
from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner, Runner, RunRequest, get_runner
from services.review.types import ReviewInput, ReviewStage


def _request(cfg: LLMConfig) -> RunRequest:
    return RunRequest(
        system_prompt="review the change",
        instructions="return findings",
        config=cfg,
        reviewers=["reviewer-a"],
    )


def _review_input() -> ReviewInput:
    return ReviewInput(
        stage=ReviewStage.PLAN_REVIEW,
        subject_ref="s",
        context_ref="c",
        prompt_name="plan-review",
        prompt_version="v1",
        system_prompt="review the plan",
        instructions="the plan",
        reviewers=("plan-reviewer",),
    )


def test_get_runner_uses_the_injected_override():
    cfg = LLMConfig(runner="fake")
    fake = FakeRunner(findings=[], summary="clean")

    runner = get_runner(cfg, override=fake)

    assert isinstance(runner, Runner)
    assert runner is fake


def test_fake_runner_is_offline_ready():
    FakeRunner().preflight()


def test_fake_runner_returns_a_validated_review_result():
    cfg = LLMConfig(runner="fake")
    runner = get_runner(cfg, override=FakeRunner(findings=[], summary="clean"))

    result = runner.run(_request(cfg))

    assert result["runner"] == "fake"
    assert result["findings"] == []
    assert result["summary"] == "clean"


def test_is_failover_exhaustion_detects_group_cause_and_nesting():
    from pydantic_ai.exceptions import FallbackExceptionGroup
    from services.review.kernel import _is_failover_exhaustion

    group = FallbackExceptionGroup("all failed", [ValueError("a")])
    assert _is_failover_exhaustion(group) is True

    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = group
    assert _is_failover_exhaustion(wrapped) is True

    nested = ExceptionGroup("outer", [group])
    assert _is_failover_exhaustion(nested) is True

    assert _is_failover_exhaustion(ValueError("unrelated")) is False


def test_block_on_failover_is_a_zero_usage_block():
    from services.review.kernel import RebarReviewKernel
    from services.review.types import TokenUsage, Verdict

    raw = RebarReviewKernel()._block_on_failover(types.SimpleNamespace(), "review-model")

    assert raw.verdict is Verdict.BLOCK
    assert raw.usage == TokenUsage()
    assert len(raw.findings) == 1
    assert raw.findings[0].blocking is True
    assert raw.provenance[0].tier == "review-model"


def test_acceptance_finder_instructions_drive_per_criterion_coverage():
    from services.review.kernel import _finder_instructions

    text = _finder_instructions(
        ReviewStage.ACCEPTANCE_VERIFICATION, "completion", "criteria", "body-under-review"
    )
    assert "body-under-review" in text
    assert "each acceptance criterion" in text
    assert "blocking finding for every criterion" in text
    assert "only when every acceptance criterion is fully satisfied" in text


def test_non_acceptance_finder_instructions_surface_defects():
    from services.review.kernel import _finder_instructions

    for stage in (
        ReviewStage.PLAN_REVIEW,
        ReviewStage.CODE_REVIEW,
        ReviewStage.SECURITY_REVIEW,
    ):
        text = _finder_instructions(stage, "change", "correctness", "body")
        assert "Surface every grounded defect" in text
        assert "acceptance criterion" not in text


def test_acceptance_decision_blocks_on_validity_not_code_impact():
    from services.review.kernel import (
        ACCEPTANCE_BLOCK_THRESHOLD,
        CODE_BLOCK_THRESHOLD,
        _decision_params,
    )

    code_impact = lambda _attrs: 0.4  # noqa: E731
    impact_fn, threshold_for = _decision_params(ReviewStage.ACCEPTANCE_VERIFICATION, code_impact)
    assert impact_fn({"anything": True}) == 1.0
    assert threshold_for([]) == (ACCEPTANCE_BLOCK_THRESHOLD, True)
    assert ACCEPTANCE_BLOCK_THRESHOLD < CODE_BLOCK_THRESHOLD


def test_non_acceptance_decision_keeps_code_impact_and_threshold():
    from services.review.kernel import CODE_BLOCK_THRESHOLD, _decision_params

    code_impact = lambda _attrs: 0.4  # noqa: E731
    for stage in (
        ReviewStage.PLAN_REVIEW,
        ReviewStage.CODE_REVIEW,
        ReviewStage.SECURITY_REVIEW,
    ):
        impact_fn, threshold_for = _decision_params(stage, code_impact)
        assert impact_fn is code_impact
        assert threshold_for([]) == (CODE_BLOCK_THRESHOLD, True)


def _patch_gate_source(monkeypatch, cfg) -> None:
    import rebar.llm.gate_source as gate_source
    import services.review.llm as review_llm

    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: object())
    monkeypatch.setattr(gate_source, "apply_handle", lambda config, handle: config)
    monkeypatch.setattr(gate_source, "gate_read_root", lambda handle: contextlib.nullcontext())
    monkeypatch.setattr(review_llm, "review_llm_config", lambda tier: cfg)


def test_run_converts_failover_exhaustion_to_a_block(monkeypatch):
    from pydantic_ai.exceptions import FallbackExceptionGroup
    from services.review.kernel import RebarReviewKernel
    from services.review.types import Verdict

    cfg = types.SimpleNamespace(model_provider="bedrock", model=None)
    _patch_gate_source(monkeypatch, cfg)

    def _raise(self, review, config, tier):
        raise FallbackExceptionGroup("all failed", [ValueError("x")])

    monkeypatch.setattr(RebarReviewKernel, "_run_multipass", _raise)

    raw = RebarReviewKernel().run(_review_input())

    assert raw.verdict is Verdict.BLOCK
    assert raw.findings[0].blocking is True


def test_run_propagates_a_non_failover_error(monkeypatch):
    from services.review.kernel import RebarReviewKernel

    cfg = types.SimpleNamespace(model_provider="bedrock", model=None)
    _patch_gate_source(monkeypatch, cfg)

    def _raise(self, review, config, tier):
        raise ValueError("a real bug")

    monkeypatch.setattr(RebarReviewKernel, "_run_multipass", _raise)

    with pytest.raises(ValueError, match="a real bug"):
        RebarReviewKernel().run(_review_input())
