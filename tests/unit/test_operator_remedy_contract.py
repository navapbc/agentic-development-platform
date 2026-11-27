"""The operator remedy contract: every alert names a next step its reader can run."""

from __future__ import annotations

import ast
import inspect
import re
import shlex
from pathlib import Path

import pytest
import yaml
from services.integration.credentials import config_template
from services.operator_remedy import (
    DEPLOYED_OPERATOR_TOOLS,
    NEXT_STEP_PREFIX,
    DeploymentPosture,
    Remedy,
    StepKind,
    command,
    render,
    with_remedy,
)
from services.orchestration import gate_decision, model_path, remedies, repair_decision

ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = ROOT / "services/orchestration/temporal_workflows/draft_pull_request.py"
_ALERT_HELPERS = ("_terminal_alert", "_park_for_gate_decision")
_HOST_TOOLS = frozenset({"inv"}) | DEPLOYED_OPERATOR_TOOLS
_TENANTS = ("", "acme")


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


def test_the_workflow_still_has_the_alert_sites_this_contract_covers():
    assert len(_alert_calls()) == 24


def test_with_remedy_appends_one_next_step_line():
    assert with_remedy("body", "do X") == f"body\n{NEXT_STEP_PREFIX} do X"


def test_with_remedy_omits_the_line_when_there_is_no_next_step():
    assert with_remedy("body", "") == "body"


@pytest.mark.parametrize(
    ("author", "kwargs", "stage"),
    [
        (remedies.implementation_remedy, {"execution_id": "exec-1"}, "implementation"),
        (remedies.branch_sync_remedy, {"branch": "feat/x"}, "branch_sync"),
        (remedies.repair_remedy, {"execution_id": "exec-1"}, "repair"),
        (remedies.issue_recheck_remedy, {}, "approval"),
    ],
)
def test_an_unknown_terminal_reason_degrades_to_generic_guidance(author, kwargs, stage):
    authored = author("a_token_no_worker_knows", issue_key="ADSER-24", **kwargs)

    assert authored == remedies.generic_remedy("ADSER-24", stage)
    assert [step.kind for step in authored.actions] == [StepKind.RESTART_RUN]


def test_a_missing_issue_key_still_renders_a_runnable_shaped_command():
    text = render(remedies.generic_remedy("", "plan"), DeploymentPosture.HOST)

    assert "`inv restart --issue <issue_key>`" in text


def test_the_pod_not_ready_remedy_is_the_last_line_of_the_message():
    """The caller prepends kubectl output, so the remedy has to be composed last or the
    ``Next step:`` line stops being the final instruction an operator reads."""
    from services.orchestration.gates.pod_runner import describe_pod_not_ready

    message = (
        "pod p in namespace n was not Ready within 60s. kubectl reported nothing. "
        f"{describe_pod_not_ready(_unschedulable(), 'p', 'n')}"
    )

    assert message.splitlines()[-1].startswith(NEXT_STEP_PREFIX)


def test_the_intake_hold_conditions_do_not_share_one_remedy():
    authored = {
        remedies.base_conflict_remedy("ADSER-24"),
        remedies.unroutable_remedy("ADSER-24", "ADSER"),
        remedies.checks_not_configured_remedy("ADSER-24", ["navapbc/demo"]),
        remedies.unresolvable_base_remedy("ADSER-24"),
    }

    assert len(authored) == 4


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

    assert emitted - narrowed == {caps.COMPLETED}


def test_every_model_budget_stop_is_a_known_repair_and_implementation_reason():
    from services.integration.litellm_admin import MODEL_BUDGET_REASONS

    assert {member.value for member in remedies.RepairReason} >= MODEL_BUDGET_REASONS
    assert {member.value for member in remedies.ImplementationReason} >= MODEL_BUDGET_REASONS


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
    assert re.search(r"\[360 lines and \d+ characters omitted\]", message.body)


def test_one_enormous_line_cannot_push_the_next_step_out_of_reach():
    """A line cap alone passes a six-line detail holding a single minified payload."""
    from services.orchestration.notifications import DETAIL_CHAR_LIMIT, bounded_detail

    bounded = bounded_detail("head\n" + "x" * 200_000 + "\ntail")

    assert len(bounded) < DETAIL_CHAR_LIMIT + 200
    assert bounded.endswith("tail")


def test_a_short_detail_is_left_intact():
    from services.orchestration.notifications import bounded_detail

    assert bounded_detail("one\ntwo") == "one\ntwo"


def _host(authored: Remedy) -> str:
    return render(authored, DeploymentPosture.HOST)


def test_an_unlisted_repository_is_not_told_to_add_a_repos_marker():
    """Intake rejects a ticket for two structurally different reasons. Naming no repository is
    fixed on the ticket. Naming one outside the allowlist is fixed in the tenant's config, and
    the marker advice tells the operator to redo the thing that already failed."""
    text = _host(remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/secret",)))

    assert "navapbc/secret" in text
    assert "`repositories` in `config/local/tenants/<tenant>/repositories.yaml`" in text
    assert "repos marker" not in text
    assert "default_repo" not in text


def test_a_ticket_naming_no_repository_is_told_where_to_name_one():
    text = _host(remedies.unroutable_remedy("ADSER-24", "ADSER"))

    assert "repos marker" in text
    assert "`default_repo` in `config/local/tenants/<tenant>/jira.yaml`" in text


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
    assert remedies.unroutable_remedy("ADSER-24", "ADSER", resolution.unlisted) != (
        remedies.unroutable_remedy("ADSER-24", "ADSER")
    )


def test_abandoning_a_repair_does_not_claim_intake_will_relaunch():
    authored = remedies.repair_remedy(
        "repair_exhausted", issue_key="ADSER-24", execution_id="exec-1"
    )

    host = _host(authored)
    assert [step.kind for step in authored.actions] == [
        StepKind.RETRY_REPAIR,
        StepKind.ABANDON_REPAIR,
    ]
    abandon = host.index("--abandon")
    assert "intake" not in host[host.rfind(".", 0, abandon) : host.find(".", abandon)]


@pytest.mark.parametrize(
    "token",
    [
        "cost_budget_exceeded",
        "token_budget_exceeded",
        "llm_calls_exceeded",
        "run_model_budget_exceeded",
        "tenant_model_budget_exceeded",
    ],
)
def test_a_repair_budget_park_offers_abandon_as_its_only_decision(token):
    """A retry at a spent budget parks again at once, so abandon is the one decision that helps."""
    authored = remedies.repair_remedy(token, issue_key="ADSER-24", execution_id="exec-1")

    decisions = [step.kind for step in authored.actions if step.kind in _SIGNALLED_DECISIONS]
    assert decisions == [StepKind.ABANDON_REPAIR]


def test_an_implementation_budget_stop_offers_no_repair_decision():
    """An implementation-stage budget stop ends the run, so no parked repair waits on abandon."""
    authored = remedies.implementation_remedy(
        "cost_budget_exceeded", issue_key="ADSER-24", execution_id="exec-1"
    )

    assert StepKind.ABANDON_REPAIR not in [step.kind for step in authored.actions]


def test_the_invalidating_remedy_names_fields_that_can_invalidate_a_run():
    """Only the identity fields pause a run. Citing assignee or repository sends the operator
    looking for a change that classifies as metadata and never stopped anything."""
    from services.integration.jira.edits import _IDENTITY_FIELDS

    text = _host(
        remedies.branch_sync_remedy("execution_invalidating", issue_key="ADSER-24", branch="x")
    )

    assert "assignee" not in text
    assert any(field.replace("_", " ") in text for field in _IDENTITY_FIELDS)


def test_a_reused_pull_request_names_the_pull_request_as_the_next_step():
    url = "https://github.com/navapbc/demo/pull/7"

    assert url in _host(remedies.existing_pr_remedy("ADSER-24", url))


def test_a_code_defect_park_tells_the_operator_the_run_is_waiting_not_dead():
    text = _host(remedies.code_defect_remedy("AttributeError: nope"))

    assert "newer build" in text
    assert "AttributeError: nope" in text


def test_a_policy_human_review_gate_is_named_the_way_the_operator_must_type_it():
    """The promotion policy emits each entry as `gate_failed:<name>`. Carried through verbatim the
    operator is told to approve a gate that no gate is called."""
    from services.orchestration.temporal_workflows.draft_pull_request import _gate_names

    assert _gate_names(["gate_failed:security-review"]) == "security-review"
    assert (
        _gate_names(["gate_failed:security-review", "gate_failed:coverage"])
        == "security-review,coverage"
    )
    assert "gate_failed" not in _host(
        remedies.gate_decision_remedy("ADSER-24", _gate_names(["gate_failed:security-review"]))
    )


def test_the_promotion_policy_is_still_the_only_producer_of_the_stripped_prefix():
    """The stripping is only correct while every human_review entry carries the prefix."""
    rego = (ROOT / "config/policies/promotion.rego").read_text()
    producers = [line for line in rego.splitlines() if line.startswith("human_review contains")]

    assert producers, "the human_review rule moved"
    assert all("gate_failed:%s" in line for line in producers)


def test_a_pod_whose_status_cannot_be_read_still_names_a_next_step():
    """The three read-failure branches short-circuit before the describe path, so they bypassed
    every remedy the pod runner attaches."""
    from services.orchestration.gates.pod_runner import _check_kubectl_access, _inspect_the_pod

    assert "kubectl get pod -n gates" in _check_kubectl_access("gates")
    assert "kubectl describe pod -n gates runner-7" in _inspect_the_pod("runner-7", "gates")


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


def _every_authored_remedy() -> dict[str, Remedy]:
    authored = {
        "unroutable_no_repo": remedies.unroutable_remedy("ADSER-24", "ADSER"),
        "unroutable_no_repo_rebar": remedies.unroutable_remedy(
            "ADSER-24", "ADSER", ticket_config="tickets"
        ),
        "unroutable_unlisted": remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/x",)),
        "existing_pr": remedies.existing_pr_remedy("ADSER-24", "https://example.invalid/pr/1"),
        "code_defect": remedies.code_defect_remedy("boom"),
        "base_conflict": remedies.base_conflict_remedy("ADSER-24"),
        "unresolvable_base": remedies.unresolvable_base_remedy("ADSER-24"),
        "checks_not_configured": remedies.checks_not_configured_remedy("ADSER-24", ["repo-a"]),
        "gate_rejected": remedies.gate_rejected_remedy("ADSER-24", "security-review"),
        "gate_decision": remedies.gate_decision_remedy("ADSER-24", "security-review"),
        "file_held": remedies.file_held_remedy(
            "ADSER-24",
            "file_claim",
            path="tsconfig.build.json",
            holder_task_id="ADSER-25",
            holder_work_unit="unit-1",
        ),
        "review_blocked": remedies.review_blocked_remedy("ADSER-24", "fail_closed"),
        "plan_rejected": remedies.plan_rejected_remedy("ADSER-24"),
        "policy_denied": remedies.policy_denied_remedy("ADSER-24", "bundle"),
        "approval_rejected": remedies.approval_rejected_remedy("ADSER-24"),
        "claim_denied": remedies.claim_denied_remedy("ADSER-24"),
        "generic": remedies.generic_remedy("ADSER-24", "implementation"),
        "run_failed": remedies.run_failed_remedy("ADSER-24", "plan"),
        "abandoned": remedies.abandoned_remedy("ADSER-24"),
        "orphan_reclaimed": remedies.orphan_reclaimed_remedy(
            "ADSER-24", "draft-pr-ADSER-24-intake-1"
        ),
    }
    for hop in model_path.HOPS:
        authored[f"model_path_unavailable:{hop}"] = remedies.model_path_unavailable_remedy(hop)
        authored[f"model_path_recovered:{hop}"] = remedies.model_path_recovered_remedy(hop)
        authored[f"model_unavailable_park:{hop}"] = remedies.model_unavailable_park_remedy(
            "ADSER-24", hop
        )
    for token in remedies.ImplementationReason:
        authored[f"implementation:{token}"] = remedies.implementation_remedy(
            str(token), issue_key="ADSER-24", execution_id="exec-1"
        )
    for token in remedies.RepairReason:
        authored[f"repair:{token}"] = remedies.repair_remedy(
            str(token), issue_key="ADSER-24", execution_id="exec-1"
        )
    for token in remedies.BranchSyncReason:
        authored[f"branch_sync:{token}"] = remedies.branch_sync_remedy(
            str(token), issue_key="ADSER-24", branch="feat/x"
        )
    for token in remedies.IssueRecheckReason:
        authored[f"issue_recheck:{token}"] = remedies.issue_recheck_remedy(
            str(token), issue_key="ADSER-24"
        )
    return authored


def _pod_remedies() -> dict[str, str]:
    from services.orchestration.gates import pod_runner

    return {
        "unschedulable": pod_runner.describe_pod_not_ready(_unschedulable(), "runner-7", "gates"),
        "image_pull": pod_runner.describe_pod_not_ready(_image_pull_failed(), "runner-7", "gates"),
        "unexplained": pod_runner.describe_pod_not_ready({"phase": "Running"}, "runner-7", "gates"),
        "kubectl_access": pod_runner._check_kubectl_access("gates"),
    }


def _rendered(posture: DeploymentPosture) -> dict[str, str]:
    texts = {
        f"{name}@{tenant or 'unbound'}": render(authored.for_tenant(tenant), posture)
        for name, authored in _every_authored_remedy().items()
        for tenant in _TENANTS
    }
    return texts | _pod_remedies()


def test_every_authored_remedy_names_at_least_one_action():
    """A code-defect park resumes on a newer build, which the operator deploys through their own
    pipeline rather than with a platform command."""
    idle = [name for name, authored in _every_authored_remedy().items() if not authored.actions]

    assert idle == ["code_defect"]


_SIGNALLED_DECISIONS = frozenset(
    {
        StepKind.RESTART_RUN,
        StepKind.RETRY_REPAIR,
        StepKind.ABANDON_REPAIR,
        StepKind.APPROVE_GATE,
        StepKind.REJECT_GATE,
    }
)


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == name)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
        )
    ]


_REASONS_A_DECISION_REACHES = ([], ["AWAITING_HUMAN"], ["LIMIT_REACHED"])


def _alerts_while_parking_where_decisions_cannot_reach() -> list[tuple[int, str, str]]:
    found = []
    for node in ast.walk(ast.parse(_WORKFLOW.read_text())):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block):
                reasons = [
                    keyword.value.attr
                    for call in _calls(statement, "Suspension")
                    for keyword in call.keywords
                    if keyword.arg == "reason" and isinstance(keyword.value, ast.Attribute)
                ]
                if not isinstance(statement, ast.Assign) or reasons in _REASONS_A_DECISION_REACHES:
                    continue
                parking = []
                for later in block[index + 1 :]:
                    if _calls(later, "await_resume"):
                        break
                    parking.append(later)
                for call in (c for later in parking for c in _calls(later, "_terminal_alert")):
                    remedy = next(kw.value for kw in call.keywords if kw.arg == "remedy")
                    assert isinstance(remedy, ast.Call) and isinstance(remedy.func, ast.Name)
                    found.append((call.lineno, reasons[0], remedy.func.id))
    return found


def test_a_park_that_no_operator_decision_reaches_names_no_decision_command():
    """`inv restart`, `inv repair`, and `inv gate` signal only the runs the executions table
    records at AWAITING_HUMAN. A run parked on another suspension, such as a code defect waiting
    for a newer build, is still running in another state, so restart signals nothing and then
    refuses because the workflow is running."""
    assert repair_decision._AWAITING_HUMAN == gate_decision._AWAITING_HUMAN == "AWAITING_HUMAN"
    alerts = _alerts_while_parking_where_decisions_cannot_reach()
    unreachable = [
        (line, reason, author)
        for line, reason, author in alerts
        for step in getattr(remedies, author)(
            *("x" for _ in inspect.signature(getattr(remedies, author)).parameters)
        ).actions
        if step.kind in _SIGNALLED_DECISIONS
    ]

    assert {
        ("AWAITING_FIX", "code_defect_remedy"),
        ("MODEL_UNAVAILABLE", "model_unavailable_park_remedy"),
    } <= {(r, a) for _, r, a in alerts}
    assert unreachable == []


def test_the_guards_render_every_remedy_the_module_exports():
    """The guards below only bind to the remedies _every_authored_remedy builds. A remedy added
    without a line there would be unchecked while the suite stayed green, so the coverage is
    asserted rather than trusted."""
    tree = ast.parse(Path(remedies.__file__).read_text())
    exported = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.endswith("_remedy")
        and not node.name.startswith("_")
    }
    rendered = set(
        re.findall(r"remedies\.(\w+_remedy)\(", inspect.getsource(_every_authored_remedy))
    )

    assert exported - rendered == set()


def _commands(text: str) -> list[list[str]]:
    """Every backticked span holding a space is a shell command, whether an action rendered it or
    prose spelled it out. Config keys, paths, and setting names render without spaces."""
    return [shlex.split(span) for span in re.findall(r"`([^`]+)`", text) if " " in span]


def _task_parameters() -> dict[str, set[str]]:
    import tasks

    return {
        name: set(inspect.signature(task.body).parameters) - {"c"}
        for name, task in tasks.ns.tasks.items()
    }


def test_every_host_command_is_an_inv_task_that_takes_its_flags():
    """A host operator works in a platform checkout. A remedy naming a task or a flag the task does
    not take leaves the operator with a command that errors out."""
    parameters = _task_parameters()
    failures = []
    for name, text in _rendered(DeploymentPosture.HOST).items():
        for argv in _commands(text):
            if argv[0] not in _HOST_TOOLS:
                failures.append((name, argv))
            elif argv[0] == "inv":
                task, flags = argv[1], [a[2:].replace("-", "_") for a in argv[2:] if a[:2] == "--"]
                if task not in parameters or not set(flags) <= parameters[task]:
                    failures.append((name, argv))

    assert failures == []


def _image_directories() -> set[str]:
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    return set(re.findall(r"^COPY (?!--)(\S+) \./\S+$", dockerfile, re.MULTILINE))


def test_every_deployed_command_uses_a_deployed_tool_and_a_module_the_image_carries():
    """A deployed operator has no checkout, so ``inv`` and ``tasks.py`` are absent where they
    read the alert. A worker command reaches the tenant's activity worker with ``kubectl exec``,
    so its module has to ship in the platform image and accept the flags the remedy passes."""
    copied = _image_directories()
    failures = []
    for name, text in _rendered(DeploymentPosture.DEPLOYED).items():
        for argv in _commands(text):
            if argv[0] not in DEPLOYED_OPERATOR_TOOLS:
                failures.append((name, argv))
                continue
            if "--" not in argv:
                continue
            inner = argv[argv.index("--") + 1 :]
            assert inner[:2] == ["python", "-m"], argv
            module = ROOT / (inner[2].replace(".", "/") + ".py")
            source = module.read_text() if module.exists() else ""
            flags = [arg for arg in inner[3:] if arg.startswith("--")]
            if (
                inner[2].split(".")[0] not in copied
                or "__main__" not in source
                or not all(f'"{flag}"' in source for flag in flags)
            ):
                failures.append((name, argv))

    assert failures == []


def test_the_deployed_rendering_names_no_checkout_location():
    """The settings files, `tasks.py`, and `config/local/` exist only in a platform checkout, and a
    path into ADP's own source asks the operator to edit the platform rather than the deployment."""
    checkout_only = re.compile(r"\.env\b|tasks\.py|\bconfig/|`inv |\b[a-z_]+(?:/[a-z_0-9]+)*\.py\b")
    offenders = {
        name: checkout_only.findall(text)
        for name, text in _rendered(DeploymentPosture.DEPLOYED).items()
        if checkout_only.search(text)
    }

    assert offenders == {}


def test_no_host_remedy_tells_the_operator_to_edit_adp_source():
    """Repository config under config/local is exempt, because the worker reads exactly that
    path."""
    source_path = re.compile(r"\b[a-z_]+(?:/[a-z_0-9]+)*\.py\b")
    offenders = {
        name: source_path.findall(text)
        for name, text in _rendered(DeploymentPosture.HOST).items()
        if source_path.search(text)
    }

    assert offenders == {}


def _template_keys(config: str) -> set[str]:
    template = config_template(config, root=ROOT)
    assert template.exists(), f"no committed template for {config}"

    def walk(node: object) -> set[str]:
        if isinstance(node, dict):
            keys: set[str] = set()
            for key, value in node.items():
                keys.add(str(key))
                keys |= walk(value)
            return keys
        if isinstance(node, list):
            return set().union(*(walk(item) for item in node))
        return set()

    return walk(yaml.safe_load(template.read_text()))


def _steps(kind: StepKind) -> set[tuple[str, str]]:
    return {
        (step.config, step.key)
        for authored in _every_authored_remedy().values()
        for step in authored.steps
        if step.kind == kind
    }


def test_every_config_key_named_in_a_remedy_exists_in_the_committed_template():
    """A remedy that names a key the config does not have sends the operator looking for
    something that is not there."""
    named = _steps(StepKind.CONFIG_KEY)

    assert named
    assert {(config, key) for config, key in named if key not in _template_keys(config)} == set()


def test_every_setting_and_constant_named_in_a_remedy_resolves():
    """Remedies that cannot name a config key name a worker setting or a module constant. Those
    drift the same way a config key does, and the operator has no way to tell a stale name from a
    current one."""
    from scripts.config import Settings
    from services.orchestration.gates import pod_runner

    env_names = {name.upper() for name in Settings.model_fields}
    settings = {key for _, key in _steps(StepKind.WORKER_SETTING)}
    constants = {
        token
        for text in _pod_remedies().values()
        for token in re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text)
    }

    assert settings and constants
    assert settings - env_names == set()
    assert constants - env_names - {name for name in vars(pod_runner) if name.isupper()} == set()


@pytest.mark.parametrize("posture", list(DeploymentPosture))
def test_every_action_renders_a_command_for_every_posture(posture):
    blank = [
        (name, step.kind)
        for name, authored in _every_authored_remedy().items()
        for step in authored.for_tenant("acme").actions
        if not command(step, posture)
    ]

    assert blank == []
