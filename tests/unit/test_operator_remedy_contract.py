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
    with_remedy,
)
from services.orchestration import remedies

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


def test_secret_not_found_renders_condition_then_remedy():
    exc = SecretNotFoundError("JIRA_API_TOKEN")

    assert exc.condition().startswith("the secret JIRA_API_TOKEN is not set")
    assert "Export JIRA_API_TOKEN" in exc.remedy
    assert str(exc) == f"{exc.condition()}\n{NEXT_STEP_PREFIX} {exc.remedy}"


def test_secret_not_found_keeps_key_error_semantics():
    exc = SecretNotFoundError("JIRA_API_TOKEN")

    assert isinstance(exc, KeyError)
    assert "Export JIRA_API_TOKEN" in str(exc), "KeyError.__str__ must not win the MRO"


def test_config_error_renders_condition_then_remedy():
    exc = ConfigError("github.yaml is version 1", remedy="Run `inv config-check`.")

    assert isinstance(exc, RuntimeError)
    assert str(exc) == f"github.yaml is version 1\n{NEXT_STEP_PREFIX} Run `inv config-check`."


def test_config_error_without_a_remedy_renders_only_the_condition():
    assert str(ConfigError("scan_scope must be a string")) == "scan_scope must be a string"


def test_unconfigured_repository_carries_a_remedy():
    exc = UnconfiguredRepositoryError("navapbc/agentic-demo-service")

    assert "github.yaml" in exc.remedy
    assert str(exc).endswith(exc.remedy)


def test_a_remediable_error_survives_the_temporal_failure_converter():
    """Temporal encodes an activity failure with ``str(exception)``, so the remedy must live
    there or it is lost before an operator ever opens the workflow in the UI."""
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter

    failure = Failure()
    DefaultFailureConverter().to_failure(
        SecretNotFoundError("JIRA_API_TOKEN"), DefaultPayloadConverter(), failure
    )

    assert NEXT_STEP_PREFIX in failure.message
    assert "Export JIRA_API_TOKEN" in failure.message


def test_the_no_next_step_sentinel_never_reaches_the_audit_record():
    """``NO_NEXT_STEP`` is an internal marker. Persisting it would put ``__no_next_step__`` in
    front of an operator reading ``audit_events`` or the Object-Locked evidence store."""
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        _audit_event_body,
    )

    event = AuditEvent(
        event_id="e",
        task_id="t",
        execution_id="x",
        event_type="HUMAN_INTERVENTION_ALERTED",
        remedy=NO_NEXT_STEP,
    )

    assert event.remedy is None
    assert _audit_event_body(event)["remedy"] is None


def test_an_authored_remedy_still_reaches_the_audit_record():
    from services.orchestration.temporal_activities.persistence import AuditEvent

    event = AuditEvent(
        event_id="e",
        task_id="t",
        execution_id="x",
        event_type="HUMAN_INTERVENTION_ALERTED",
        remedy="Run `inv restart --issue ADSER-24`.",
    )

    assert event.remedy == "Run `inv restart --issue ADSER-24`."


def test_the_pod_not_ready_remedy_is_the_last_line_of_the_message():
    """The caller appends kubectl output, so the remedy has to be composed last or the
    ``Next step:`` line stops being the final instruction an operator reads."""
    from services.orchestration.gates.pod_runner import describe_pod_not_ready

    status = {
        "phase": "Pending",
        "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
    }
    message = (
        f"pod p in namespace n was not Ready within 60s. kubectl reported nothing. "
        f"{describe_pod_not_ready(status)}"
    )

    assert message.splitlines()[-1].startswith(NEXT_STEP_PREFIX)


@pytest.mark.parametrize(
    ("author", "kwargs"),
    [
        (remedies.base_conflict_remedy, {"issue_key": "ADSER-24"}),
        (remedies.unroutable_remedy, {"issue_key": "ADSER-24", "project": "ADSER"}),
        (
            remedies.checks_not_configured_remedy,
            {"issue_key": "ADSER-24", "repos": ["navapbc/demo"]},
        ),
        (remedies.unresolvable_base_remedy, {"issue_key": "ADSER-24"}),
    ],
)
def test_each_intake_hold_condition_names_a_distinct_runnable_command(author, kwargs):
    text = author(**kwargs)

    assert "inv restart --issue ADSER-24" in text
    assert text.rstrip().endswith(".")


def test_the_intake_hold_conditions_do_not_share_one_remedy():
    texts = {
        remedies.base_conflict_remedy("ADSER-24"),
        remedies.unroutable_remedy("ADSER-24", "ADSER"),
        remedies.checks_not_configured_remedy("ADSER-24", ["navapbc/demo"]),
        remedies.unresolvable_base_remedy("ADSER-24"),
    }

    assert len(texts) == 4
