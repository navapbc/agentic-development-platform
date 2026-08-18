"""Unit tests for the deterministic CI-failure repair planner."""

from __future__ import annotations

from adapters.interfaces.ci_host import failure_summary
from rebar.llm.runner import FakeRunner
from services.orchestration.langgraph.ci_repair import (
    ACTIONABLE,
    BUILD,
    LINT,
    SECURITY,
    TEST_FAILURE,
    UNKNOWN,
    UNREPAIRABLE,
    analyze_failure,
    classify_failure,
    plan_repair,
)

_BANDIT_LOG = (
    "+ bandit -r app\n"
    "Test results:\n"
    ">> Issue: [B602:subprocess_popen_with_shell_equals_true] subprocess call with "
    "shell=True identified, security issue.\n"
    "   Severity: High   Confidence: High\n"
    "ERROR: script returned exit code 1\n"
    "Finished: FAILURE\n"
)

_RUFF_LOG = (
    "+ ruff check .\n"
    "B904 Within an `except` clause, raise exceptions with `raise ... from err`\n"
    "Found 2 errors.\n"
    "ERROR: script returned exit code 1\n"
    "Finished: FAILURE\n"
)


def test_failure_summary_captures_a_bandit_finding_and_classifies_security():
    summary = failure_summary(_BANDIT_LOG)

    assert "shell=True" in summary
    assert "Severity: High" in summary
    assert classify_failure(summary) == SECURITY


def test_failure_summary_captures_a_ruff_finding_and_classifies_lint():
    summary = failure_summary(_RUFF_LOG)

    assert "ruff check" in summary
    assert classify_failure(summary) == LINT


def test_plan_repair_classifies_a_test_failure():
    plan = plan_repair("tests/test_x.py::test_x FAILED\nAssertionError: expected 1 got 2")

    assert plan.status == ACTIONABLE
    assert plan.category == TEST_FAILURE
    assert "AssertionError" in plan.instruction


def test_plan_repair_classifies_a_lint_failure():
    plan = plan_repair("app.py:1:1: E501 line too long, ruff would reformat")

    assert plan.status == ACTIONABLE
    assert plan.category == LINT


def test_plan_repair_classifies_a_security_failure():
    plan = plan_repair(
        "+ bandit -r app\n"
        ">> Issue: [B602] subprocess call with shell=True identified, security issue.\n"
        "Severity: High Confidence: High"
    )

    assert plan.status == ACTIONABLE
    assert plan.category == SECURITY
    assert "nosec" in plan.instruction


def test_plan_repair_classifies_a_build_failure():
    plan = plan_repair('  File "app.py", line 3\n    def (\nSyntaxError: invalid syntax')

    assert plan.status == ACTIONABLE
    assert plan.category == BUILD


def test_plan_repair_aborts_on_empty_summary():
    plan = plan_repair("   ")

    assert plan.status == UNREPAIRABLE
    assert plan.category == UNKNOWN
    assert plan.instruction == ""


def test_plan_repair_aborts_on_unrecognized_summary():
    plan = plan_repair("the build queue was cancelled by an operator")

    assert plan.status == UNREPAIRABLE
    assert plan.category == UNKNOWN


def test_analyze_failure_uses_model_instruction_when_enabled():
    runner = FakeRunner(summary="Add a null guard in parser.py before indexing.")

    plan = analyze_failure(
        "tests/test_x.py::test_x FAILED\nAssertionError: expected 1 got 2",
        use_model=True,
        runner=runner,
    )

    assert plan.status == ACTIONABLE
    assert plan.category == TEST_FAILURE
    assert plan.instruction == "Add a null guard in parser.py before indexing."


def test_analyze_failure_falls_back_to_template_on_model_error():
    class _Boom:
        def run(self, req):
            raise RuntimeError("no model")

    plan = analyze_failure(
        "app.py:1:1: E501 line too long, ruff would reformat",
        use_model=True,
        runner=_Boom(),
    )

    assert plan.status == ACTIONABLE
    assert plan.category == LINT
    assert "reformat" in plan.instruction


def test_analyze_failure_skips_model_when_disabled():
    runner = FakeRunner(summary="should not be used")

    plan = analyze_failure(
        "tests/test_x.py::test_x FAILED\nAssertionError: boom",
        use_model=False,
        runner=runner,
    )

    assert plan.category == TEST_FAILURE
    assert plan.instruction != "should not be used"


def test_analyze_failure_guardrail_aborts_unknown_without_model():
    runner = FakeRunner(summary="should not be used")

    plan = analyze_failure(
        "the build queue was cancelled by an operator",
        use_model=True,
        runner=runner,
    )

    assert plan.status == UNREPAIRABLE
    assert plan.instruction == ""
