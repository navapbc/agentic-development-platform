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
        f"{describe_pod_not_ready(status, 'p', 'n')}"
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


def test_every_runtime_cap_outcome_is_a_known_implementation_reason():
    """``assert_never`` proves the match covers the enum, not that the enum covers the producers.

    ``openhands/runtime/caps.py`` is the external producer most likely to grow a new outcome, and
    one added there type-checks clean while degrading to the generic remedy. Reading its constants
    from the module rather than restating them is what makes this catch the drift.
    """
    from openhands.runtime import caps

    emitted = {
        value
        for name, value in vars(caps).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("_")
    }
    narrowed = {member.value for member in remedies.ImplementationReason}

    assert emitted - narrowed == {caps.COMPLETED}, (
        "a new runtime cap outcome needs a matching ImplementationReason member, or the alert "
        "for it silently falls back to the generic remedy"
    )


def test_a_long_check_log_cannot_push_the_next_step_out_of_reach():
    """A check stage carries its CI log as the alert detail. Rendered whole it buries the one
    actionable sentence hundreds of lines down a Jira comment."""
    from services.orchestration.notifications import (
        DETAIL_LINE_LIMIT,
        HumanInterventionAlert,
        _intervention_message,
    )

    message = _intervention_message(
        HumanInterventionAlert(
            task_id="ADSER-24",
            execution_id="exec-1",
            workflow_id="wf-1",
            repo="navapbc/demo",
            stage="checks",
            reason="checks_failed",
            detail="\n".join(f"log line {n}" for n in range(400)),
            issue_key="ADSER-24",
            remedy="Run `inv repair --issue ADSER-24 --retry`.",
        )
    )
    lines = message.body.splitlines()

    assert lines[-1].startswith(NEXT_STEP_PREFIX)
    assert len(lines) < DETAIL_LINE_LIMIT + 10
    assert "log line 399" in message.body, "the tail of the log is what diagnoses the failure"
    assert "[360 earlier lines omitted]" in message.body


def test_a_short_detail_is_left_intact():
    from services.orchestration.notifications import _bounded_detail

    assert _bounded_detail("one\ntwo") == "one\ntwo"


def test_an_unlisted_repository_is_not_told_to_add_a_repos_marker():
    """Intake rejects a ticket for two structurally different reasons. Naming no repository is
    fixed on the ticket. Naming one outside the allowlist is fixed in the environment config, and
    the marker advice tells the operator to redo the thing that already failed."""
    text = remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/secret",))

    assert "navapbc/secret" in text
    assert "allowed_repos" in text
    assert "repos marker" not in text
    assert "default_repo" not in text


def test_a_ticket_naming_no_repository_is_told_where_to_name_one():
    text = remedies.unroutable_remedy("ADSER-24", "ADSER")

    assert "repos marker" in text
    assert "allowed_repos" not in text


def test_the_unlisted_repositories_reach_the_remedy_from_target_resolution():
    """The two causes are only distinguishable at the resolver. If the discriminator stops being
    carried, both alerts collapse back onto the marker advice."""
    from services.integration.jira.intake_targets import resolve_targets

    resolution = resolve_targets(
        "repos: navapbc/secret",
        default_repo="",
        allowlist=frozenset({"navapbc/demo"}),
    )

    assert not resolution.routable
    assert resolution.unlisted == ("navapbc/secret",)
    assert "allowed_repos" in remedies.unroutable_remedy("ADSER-24", "ADSER", resolution.unlisted)


def test_abandoning_a_repair_does_not_claim_intake_will_relaunch():
    """`--abandon` signals the workflow and leaves the intake dedup record in place, so intake
    skips the ticket forever after. Only `inv restart` clears it."""
    text = remedies.repair_remedy("repair_exhausted", issue_key="ADSER-24", execution_id="exec-1")

    assert "--abandon" not in text
    assert "inv restart --issue ADSER-24" in text


def test_the_invalidating_remedy_names_fields_that_can_invalidate_a_run():
    """Only the identity fields pause a run. Citing assignee or repository sends the operator
    looking for a change that classifies as metadata and never stopped anything."""
    from services.integration.jira.edits import _IDENTITY_FIELDS

    text = remedies.branch_sync_remedy(
        "execution_invalidating", issue_key="ADSER-24", branch="feat/x"
    )

    assert "assignee" not in text
    assert any(field.replace("_", " ") in text for field in _IDENTITY_FIELDS)


def test_a_reused_pull_request_names_the_pull_request_as_the_next_step():
    """The run stops under a human-intervention headline, so the sentinel leaves the operator with
    a stop and no action when there is one."""
    text = remedies.existing_pr_remedy("ADSER-24", "https://github.com/navapbc/demo/pull/7")

    assert text != NO_NEXT_STEP
    assert "https://github.com/navapbc/demo/pull/7" in text


def test_a_policy_human_review_gate_is_named_the_way_the_operator_must_type_it():
    """The promotion policy emits each entry as `gate_failed:<name>`. Carried through verbatim the
    operator is told to approve a gate that no gate is called."""
    from services.orchestration.temporal_workflows.draft_pull_request import _gate_names

    assert _gate_names(["gate_failed:security-review"]) == "security-review"
    assert (
        _gate_names(["gate_failed:security-review", "gate_failed:coverage"])
        == "security-review,coverage"
    )
    assert "gate_failed" not in remedies.gate_decision_remedy(
        "ADSER-24", _gate_names(["gate_failed:security-review"])
    )


def test_the_promotion_policy_is_still_the_only_producer_of_the_stripped_prefix():
    """The stripping is only correct while every human_review entry carries the prefix."""
    rego = (Path(__file__).resolve().parents[2] / "config/policies/promotion.rego").read_text()
    producers = [line for line in rego.splitlines() if line.startswith("human_review contains")]

    assert producers, "the human_review rule moved"
    assert all("gate_failed:%s" in line for line in producers)


def test_a_pod_whose_status_cannot_be_read_still_names_a_next_step():
    """The three read-failure branches short-circuit before the describe path, so they bypassed
    every remedy the pod runner attaches."""
    from services.orchestration.gates.pod_runner import _check_kubectl_access, _inspect_the_pod

    assert NEXT_STEP_PREFIX in with_remedy("x", _check_kubectl_access("gates"))
    assert "gates" in _check_kubectl_access("gates")
    assert "<namespace>" not in _inspect_the_pod("runner-7", "gates")
    assert "kubectl describe pod -n gates runner-7" in _inspect_the_pod("runner-7", "gates")


def test_one_enormous_line_cannot_push_the_next_step_out_of_reach():
    """A line cap alone passes a six-line detail holding a single minified payload."""
    from services.orchestration.notifications import DETAIL_CHAR_LIMIT, _bounded_detail

    bounded = _bounded_detail("head\n" + "x" * 200_000 + "\ntail")

    assert len(bounded) < DETAIL_CHAR_LIMIT + 200
    assert bounded.endswith("tail")
