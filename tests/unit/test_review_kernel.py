"""Unit tests for the rebar review-kernel runner seam."""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from rebar.llm.runner import FakeRunner, Runner, RunRequest, get_runner


def _request(cfg: LLMConfig) -> RunRequest:
    return RunRequest(
        system_prompt="review the change",
        instructions="return findings",
        config=cfg,
        reviewers=["reviewer-a"],
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
