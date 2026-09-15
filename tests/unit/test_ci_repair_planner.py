"""Unit tests for the deterministic CI-failure repair planner."""

from __future__ import annotations

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
    failure_summary,
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


def test_lint_failure_is_not_misclassified_by_dependency_install_noise():
    summary = failure_summary(
        "Requirement already satisfied: bandit>=1.7 in ./.venv/lib/python3.13/site-packages\n"
        "+ ruff check .\n"
        "B904 Within an `except` clause, raise exceptions with `raise ... from err`\n"
        "Found 2 errors.\n"
        "ERROR: script returned exit code 1\n"
        "Finished: FAILURE\n"
    )

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


_PRETTIER_LOG = (
    "> portal@1.0.0 format-check\n"
    "> prettier --check .\n"
    "\n"
    "npm warn deprecated rimraf@3.0.2: no longer supported\n"
    "Checking formatting...\n"
    "[warn] src/pages/applications/phone-number.tsx\n"
    "[warn] Code style issues found in the above file. Run Prettier with --write to fix.\n"
    "Error: exit status 1\n"
)


def test_prettier_failure_classifies_as_lint():
    assert classify_failure(failure_summary(_PRETTIER_LOG)) == LINT


def test_prettier_summary_keeps_the_offending_filename_for_the_repair_agent():
    summary = failure_summary(_PRETTIER_LOG)

    assert "src/pages/applications/phone-number.tsx" in summary


def test_prettier_summary_drops_npm_deprecation_noise():
    assert "npm warn deprecated" not in failure_summary(_PRETTIER_LOG)


def test_eslint_failure_classifies_as_lint():
    summary = failure_summary(
        "/workspace/portal/src/x.tsx\n"
        "  64:7  error  Unexpected token  react/jsx-no-undef\n"
        "\u2716 1 problem (1 error, 0 warnings)\n"
    )

    assert classify_failure(summary) == LINT


def test_typescript_compile_error_classifies_as_build():
    summary = failure_summary(
        "src/x.tsx(64,7): error TS2322: Type 'string' is not assignable.\nError: exit status 1\n"
    )

    assert classify_failure(summary) == BUILD


def test_jest_failure_still_classifies_as_a_test_failure():
    summary = failure_summary(
        "FAIL tests/pages/applications/phone-number.test.ts\n"
        "Tests:       1 failed, 0 passed, 1 total\n"
        "Error: exit status 1\n"
    )

    assert classify_failure(summary) == TEST_FAILURE


def test_failing_files_keeps_dotfiles_and_dot_directories():
    from services.orchestration.langgraph.ci_repair import failing_files, in_repair_scope

    found = failing_files("[warn] .eslintrc.js\n[warn] .github/workflows/ci.yml")

    assert found == (".eslintrc.js", ".github/workflows/ci.yml")
    assert in_repair_scope("portal/.eslintrc.js", found)


def test_scope_rejects_a_changed_path_shorter_than_the_entry_it_resembles():
    from services.orchestration.langgraph.ci_repair import in_repair_scope

    scope = ("portal/src/app.tsx",)

    assert in_repair_scope("portal/src/app.tsx", scope)
    assert in_repair_scope("other/portal/src/app.tsx", scope)
    assert not in_repair_scope("src/app.tsx", scope)
    assert not in_repair_scope("app.tsx", scope)


def test_prose_and_urls_do_not_widen_the_repair_scope():
    from services.orchestration.langgraph.ci_repair import failing_files

    found = failing_files("see https://example.com/x.html, e.g. portal/src/app.tsx is broken")

    assert found == ("portal/src/app.tsx",)


_LINT_SUMMARY = "app.py:1:1: E501 line too long, ruff would reformat"


def test_a_declared_fix_command_is_named_in_the_lint_instruction():
    """The instruction is the only channel the repair agent reads, so the hint must reach it."""
    plan = plan_repair(_LINT_SUMMARY, fix_command="npm run lint:fix")

    assert plan.category == LINT
    assert "run `npm run lint:fix`" in plan.instruction


def test_the_lint_instruction_reads_cleanly_when_no_fix_command_is_declared():
    """A project without a fix command must not be told to run an empty one."""
    plan = plan_repair(_LINT_SUMMARY)

    assert plan.category == LINT
    assert "fix command" not in plan.instruction
    assert "{fix_hint}" not in plan.instruction
    assert "report. \n" not in plan.instruction


def test_a_declared_fix_command_does_not_leak_into_other_categories():
    plan = plan_repair(
        '  File "app.py", line 3\n    def (\nSyntaxError: invalid syntax',
        fix_command="npm run lint:fix",
    )

    assert "npm run lint:fix" not in plan.instruction
