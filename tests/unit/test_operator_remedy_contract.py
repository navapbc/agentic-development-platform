"""The operator remedy contract: every human-intervention alert names a next step."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from services.integration.credentials import ConfigError, SecretNotFoundError
from services.integration.github.credentials import UnconfiguredRepositoryError
from services.operator_remedy import (
    NEXT_STEP_PREFIX,
    NO_NEXT_STEP,
    operator_text,
    with_remedy,
)
from services.orchestration.temporal_workflows import remedies

_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "services/orchestration/temporal_workflows/draft_pull_request.py"
)
_ALERT_HELPERS = ("_terminal_alert", "_park_for_gate_decision")


def _alert_calls() -> list[tuple[int, str, ast.Call]]:
    tree = ast.parse(_WORKFLOW.read_text())
    return [
        (node.lineno, node.func.attr, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _ALERT_HELPERS
    ]


def test_every_alert_site_supplies_a_remedy():
    missing = [
        (line, name)
        for line, name, call in _alert_calls()
        if not any(kw.arg == "remedy" for kw in call.keywords)
    ]
    assert missing == []


def test_no_alert_site_supplies_an_empty_remedy():
    empty = [
        (line, name)
        for line, name, call in _alert_calls()
        for kw in call.keywords
        if kw.arg == "remedy"
        and isinstance(kw.value, ast.Constant)
        and not str(kw.value.value).strip()
    ]
    assert empty == []


def test_the_workflow_still_has_the_alert_sites_this_contract_covers():
    assert len(_alert_calls()) == 17


def test_with_remedy_appends_one_next_step_line():
    assert with_remedy("body", "do X") == f"body\n{NEXT_STEP_PREFIX} do X"


@pytest.mark.parametrize("remedy", ["", NO_NEXT_STEP])
def test_with_remedy_omits_the_line_when_there_is_no_next_step(remedy):
    assert with_remedy("body", remedy) == "body"


@pytest.mark.parametrize(
    ("render", "kwargs", "stage"),
    [
        (remedies.implementation_remedy, {"execution_id": "exec-1"}, "implementation"),
        (remedies.branch_sync_remedy, {"branch": "feat/x"}, "branch_sync"),
        (remedies.repair_remedy, {"execution_id": "exec-1"}, "repair"),
        (remedies.issue_recheck_remedy, {}, "approval"),
    ],
)
def test_an_unknown_terminal_reason_degrades_to_generic_guidance(render, kwargs, stage):
    text = render("a_token_no_worker_knows", issue_key="ADSER-24", **kwargs)

    assert text == remedies.generic_remedy("ADSER-24", stage)
    assert "inv restart --issue ADSER-24" in text


@pytest.mark.parametrize(
    ("enum", "render", "kwargs"),
    [
        (
            remedies.ImplementationReason,
            remedies.implementation_remedy,
            {"execution_id": "exec-1"},
        ),
        (remedies.BranchSyncReason, remedies.branch_sync_remedy, {"branch": "feat/x"}),
        (remedies.RepairReason, remedies.repair_remedy, {"execution_id": "exec-1"}),
        (remedies.IssueRecheckReason, remedies.issue_recheck_remedy, {}),
    ],
)
def test_every_known_terminal_reason_renders_a_non_empty_remedy(enum, render, kwargs):
    for member in enum:
        assert render(member.value, issue_key="ADSER-24", **kwargs).strip()


def test_a_missing_issue_key_still_renders_a_runnable_shaped_command():
    assert "inv restart --issue <issue_key>" in remedies.generic_remedy("", "plan")


def test_secret_not_found_states_the_condition_without_the_remedy():
    exc = SecretNotFoundError("JIRA_API_TOKEN")

    assert "is not set in the process environment" in str(exc)
    assert "Export" not in str(exc)
    assert "Export JIRA_API_TOKEN" in exc.remedy
    assert operator_text(exc) == f"{exc}\n{NEXT_STEP_PREFIX} {exc.remedy}"


def test_config_error_states_the_condition_without_the_remedy():
    exc = ConfigError("github.yaml is version 1", remedy="Run `inv config-check`.")

    assert "inv config-check" not in str(exc)
    assert NEXT_STEP_PREFIX in operator_text(exc)


def test_unconfigured_repository_carries_a_remedy():
    exc = UnconfiguredRepositoryError("navapbc/agentic-demo-service")

    assert "config-check" not in str(exc)
    assert "github.yaml" in exc.remedy
    assert NEXT_STEP_PREFIX in operator_text(exc)


def test_operator_text_passes_through_an_error_with_no_remedy():
    assert operator_text(ValueError("plain")) == "plain"
