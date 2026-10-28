"""The operator remedy contract: every human-intervention alert names a next step."""

from __future__ import annotations

import ast
import inspect
import re
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
    assert len(_alert_calls()) == 18


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
    assert "allowlist in config/<environment>/github.yaml" in text
    assert "repos marker" not in text
    assert "default_repo" not in text


def test_a_ticket_naming_no_repository_is_told_where_to_name_one():
    text = remedies.unroutable_remedy("ADSER-24", "ADSER")

    assert "repos marker" in text
    assert "allowlist" not in text


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
    assert "allowlist" in remedies.unroutable_remedy("ADSER-24", "ADSER", resolution.unlisted)


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


def _config_key_calls() -> list[tuple[str, str]]:
    sources = [
        Path(__file__).resolve().parents[2] / "services/orchestration/remedies.py",
        Path(__file__).resolve().parents[2] / "services/integration/github/credentials.py",
    ]
    calls: list[tuple[str, str]] = []
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "config_key"
            ):
                args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                assert len(args) == 2, f"config_key takes two literals, got {args}"
                calls.append((str(args[0]), str(args[1])))
    return calls


def _template_keys(config: str) -> set[str]:
    import yaml

    template = Path(__file__).resolve().parents[2] / f"config/local/{config}.yaml.example"
    assert template.exists(), f"no committed template for {config}"

    def walk(node: object) -> set[str]:
        if isinstance(node, dict):
            keys: set[str] = set()
            for key, value in node.items():
                keys.add(str(key))
                keys |= walk(value)
            return keys
        if isinstance(node, list):
            return set().union(*(walk(item) for item in node)) if node else set()
        return set()

    return walk(yaml.safe_load(template.read_text()))


def test_every_config_key_named_in_a_remedy_exists_in_the_committed_template():
    """A remedy that names a key the config does not have sends the operator looking for
    something that is not there. One such key reached review: the remedy said `allowed_repos`,
    which is the name of the Python reader, while the key is `allowlist`."""
    calls = _config_key_calls()

    assert calls, "the config_key helper is how remedies name config keys"
    for config, key in calls:
        assert key in _template_keys(config), (
            f"remedy names {key} in {config}.yaml, which the committed template does not have"
        )


def test_every_command_named_in_a_remedy_is_a_registered_invoke_task():
    """A remedy naming a task that does not exist leaves the operator with a command that errors
    out. The command is the whole point of attaching a remedy."""
    import re

    import tasks

    texts = [
        remedies.unroutable_remedy("ADSER-24", "ADSER"),
        remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/x",)),
        remedies.existing_pr_remedy("ADSER-24", "https://example.invalid/pr/1"),
        remedies.code_defect_remedy("ADSER-24", "boom"),
        remedies.base_conflict_remedy("ADSER-24"),
        remedies.unresolvable_base_remedy("ADSER-24"),
        remedies.checks_not_configured_remedy("ADSER-24", ["repo-a"]),
        remedies.gate_rejected_remedy("ADSER-24", "security-review"),
        remedies.gate_decision_remedy("ADSER-24", "security-review"),
        remedies.review_blocked_remedy("ADSER-24", "fail_closed"),
        remedies.plan_rejected_remedy("ADSER-24"),
        remedies.policy_denied_remedy("ADSER-24", "bundle"),
        remedies.approval_rejected_remedy("ADSER-24"),
        remedies.claim_denied_remedy("ADSER-24"),
        *(
            remedies.implementation_remedy(token, issue_key="ADSER-24", execution_id="exec-1")
            for token in ("cost_budget_exceeded", "tool_calls_exceeded", "wall_clock_exceeded")
        ),
        *(
            remedies.repair_remedy(token, issue_key="ADSER-24", execution_id="exec-1")
            for token in ("repair_exhausted", "human_abandoned")
        ),
    ]
    registered = {task.name.replace("-", "_") for task in tasks.ns.tasks.values()}
    named = {
        name.replace("-", "_") for text in texts for name in re.findall(r"`inv ([a-z0-9-]+)", text)
    }

    assert named, "the remedies under test name at least one command"
    assert named <= registered, f"remedies name unregistered tasks: {sorted(named - registered)}"


def test_a_code_defect_park_tells_the_operator_the_run_is_waiting_not_dead():
    """The push_branch code-defect path parks the ticket and waits for a newer build. Without an
    alert the operator sees a ticket silently moved to the parked status."""
    text = remedies.code_defect_remedy("ADSER-24", "AttributeError: nope")

    assert text != NO_NEXT_STEP
    assert "newer build" in text
    assert "AttributeError: nope" in text


def test_every_setting_and_constant_named_in_a_remedy_resolves():
    """Remedies that cannot name a config key name an environment variable or a module constant
    instead. Those drift the same way a config key does, and the operator has no way to tell a
    stale name from a current one."""
    import re

    from scripts.config import Settings
    from services.orchestration.gates import pod_runner

    texts = [
        remedies.implementation_remedy(token, issue_key="ADSER-24", execution_id="exec-1")
        for token in ("cost_budget_exceeded", "tool_calls_exceeded", "wall_clock_exceeded")
    ]
    texts += [
        pod_runner.describe_pod_not_ready(_unschedulable(), "runner-7", "gates"),
        pod_runner.describe_pod_not_ready(_image_pull_failed(), "runner-7", "gates"),
    ]
    env_names = {name.upper() for name in Settings.model_fields}
    constants = {name for name in vars(pod_runner) if name.isupper()}
    named = {
        token
        for text in texts
        for token in re.findall(r"\b[A-Z][A-Z0-9_]{4,}\b", text)
        if "_" in token
    }

    assert named, "the remedies under test name at least one setting or constant"
    unresolved = {token for token in named if token not in env_names and token not in constants}
    assert not unresolved, f"remedies name settings or constants that do not exist: {unresolved}"


def _unschedulable() -> dict:
    return {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/1 nodes are available: Insufficient cpu.",
            }
        ],
    }


def _image_pull_failed() -> dict:
    return {
        "phase": "Pending",
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [
            {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "back-off"}}}
        ],
    }


def _every_rendered_remedy() -> dict[str, str]:
    from services.orchestration.gates import pod_runner

    texts = {
        "unroutable_no_repo": remedies.unroutable_remedy("ADSER-24", "ADSER"),
        "unroutable_unlisted": remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/x",)),
        "existing_pr": remedies.existing_pr_remedy("ADSER-24", "https://example.invalid/pr/1"),
        "code_defect": remedies.code_defect_remedy("ADSER-24", "boom"),
        "base_conflict": remedies.base_conflict_remedy("ADSER-24"),
        "unresolvable_base": remedies.unresolvable_base_remedy("ADSER-24"),
        "checks_not_configured": remedies.checks_not_configured_remedy("ADSER-24", ["repo-a"]),
        "gate_rejected": remedies.gate_rejected_remedy("ADSER-24", "security-review"),
        "gate_decision": remedies.gate_decision_remedy("ADSER-24", "security-review"),
        "review_blocked": remedies.review_blocked_remedy("ADSER-24", "fail_closed"),
        "plan_rejected": remedies.plan_rejected_remedy("ADSER-24"),
        "policy_denied": remedies.policy_denied_remedy("ADSER-24", "bundle"),
        "approval_rejected": remedies.approval_rejected_remedy("ADSER-24"),
        "claim_denied": remedies.claim_denied_remedy("ADSER-24"),
        "generic": remedies.generic_remedy("ADSER-24", "implementation"),
        "unschedulable": pod_runner.describe_pod_not_ready(_unschedulable(), "runner-7", "gates"),
        "image_pull": pod_runner.describe_pod_not_ready(_image_pull_failed(), "runner-7", "gates"),
    }
    for token in remedies.ImplementationReason:
        texts[f"implementation:{token}"] = remedies.implementation_remedy(
            str(token), issue_key="ADSER-24", execution_id="exec-1"
        )
    for token in remedies.RepairReason:
        texts[f"repair:{token}"] = remedies.repair_remedy(
            str(token), issue_key="ADSER-24", execution_id="exec-1"
        )
    for token in remedies.BranchSyncReason:
        texts[f"branch_sync:{token}"] = remedies.branch_sync_remedy(
            str(token), issue_key="ADSER-24", branch="feat/x"
        )
    for token in remedies.IssueRecheckReason:
        texts[f"issue_recheck:{token}"] = remedies.issue_recheck_remedy(
            str(token), issue_key="ADSER-24"
        )
    return texts


def test_no_remedy_sends_the_operator_to_a_local_only_location():
    """ADP is deployed to a container platform as well as run locally. `.env` is the local secret
    and settings file, and docs/local-to-aws-mapping.md maps it to Secrets Manager or SSM in a
    deployed environment, so a remedy naming it is wrong for every deployment that is not a
    laptop. Name the setting and let the operator apply it where their environment supplies it."""
    offenders = {
        name: text for name, text in _every_rendered_remedy().items() if ".env" in text
    }

    assert not offenders, f"remedies send the operator to .env: {sorted(offenders)}"


def test_no_remedy_tells_the_operator_to_edit_adp_source():
    """A remedy naming a path inside ADP's own tree asks the operator to edit the platform rather
    than their deployment, which needs a new build and is not an operator action. Repository
    config under config/<environment> is exempt, because load_env_config reads exactly that path
    in every environment."""
    source_path = re.compile(r"\b[a-z_]+(?:/[a-z_0-9]+)*\.py\b")
    offenders = {
        name: source_path.findall(text)
        for name, text in _every_rendered_remedy().items()
        if source_path.search(text)
    }

    assert not offenders, f"remedies name ADP source files: {offenders}"


def test_the_location_guards_render_every_remedy_the_module_exports():
    """The two guards above only bind to remedies _every_rendered_remedy builds. A remedy added
    without a line there would be unchecked while the suite stayed green, so the coverage is
    asserted rather than trusted."""
    tree = ast.parse((Path(remedies.__file__)).read_text())
    exported = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.endswith("_remedy")
        and not node.name.startswith("_")
    }
    source = inspect.getsource(_every_rendered_remedy)
    rendered = set(re.findall(r"remedies\.(\w+_remedy)\(", source))

    assert not exported - rendered, f"remedies never rendered by the guards: {exported - rendered}"
