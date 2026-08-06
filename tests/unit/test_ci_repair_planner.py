"""Unit tests for the deterministic CI-failure repair planner."""

from __future__ import annotations

from services.orchestration.langgraph.ci_repair import (
    ACTIONABLE,
    BUILD,
    LINT,
    TEST_FAILURE,
    UNKNOWN,
    UNREPAIRABLE,
    plan_repair,
)


def test_plan_repair_classifies_a_test_failure():
    plan = plan_repair("tests/test_x.py::test_x FAILED\nAssertionError: expected 1 got 2")

    assert plan.status == ACTIONABLE
    assert plan.category == TEST_FAILURE
    assert "AssertionError" in plan.instruction


def test_plan_repair_classifies_a_lint_failure():
    plan = plan_repair("app.py:1:1: E501 line too long, ruff would reformat")

    assert plan.status == ACTIONABLE
    assert plan.category == LINT


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
