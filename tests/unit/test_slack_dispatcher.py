from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import pytest
from services.integration.slack.dispatcher import (
    DecisionRejected,
    RecordedDecision,
    Refusal,
    SlackClick,
    SlackDispatcher,
    TemporalWorkflowDecisions,
    click_from_payload,
)
from services.integration.slack.tokens import ActionToken, park_digest, sign_action_token
from services.orchestration.escalation import SlackSettings, SlackUser
from services.orchestration.temporal_workflows.draft_pull_request import (
    DraftPullRequestWorkflow,
    ParkDecisionOutcome,
)
from temporalio.client import WorkflowUpdateRPCTimeoutOrCancelledError

KEY = b"alpha-action-key"
PARK = "intake-1a2b3c4d:review:security-review:1"
WORKFLOW = "draft-ADSER-24"


def _settings(**overrides) -> SlackSettings:
    fields = {
        "transport": "socket",
        "channel": "C-ESCALATE",
        "organization_id": "E-ORG",
        "app_id": "A-APP",
        "profile": "private",
        "content": "full",
        "allowlist": {
            "gate": (SlackUser("E-ORG", "U-LEAD"), SlackUser("E-ORG", "U-OTHER")),
            "repair": (SlackUser("E-ORG", "U-OPS"),),
        },
    }
    return SlackSettings(**{**fields, **overrides})


def _value(**overrides) -> str:
    fields = {
        "tenant": "alpha",
        "execution_id": "intake-1a2b3c4d",
        "workflow_id": WORKFLOW,
        "park_key": PARK,
        "kind": "gate",
        "actions": ("approve",),
        "expires_at": int(time.time()) + 3600,
    }
    return sign_action_token(ActionToken(**{**fields, **overrides}), KEY)


def _click(value: str | None = None, **overrides) -> SlackClick:
    fields = {
        "value": _value() if value is None else value,
        "action": "approve",
        "organization": "E-ORG",
        "user": "U-LEAD",
        "channel": "C-ESCALATE",
    }
    return SlackClick(**{**fields, **overrides})


@dataclass
class _Decisions:
    outcome: ParkDecisionOutcome | None = None
    reject: bool = False
    delay: float = 0.0
    waiting: str = ""
    applied: list[tuple] = field(default_factory=list)

    async def apply(self, workflow_id, update, args, update_id):
        self.applied.append((workflow_id, update, tuple(args), update_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.reject:
            raise DecisionRejected("stale")
        assert self.outcome is not None
        return self.outcome

    async def waiting_park(self, workflow_id):
        return self.waiting


@dataclass
class _Records:
    tenants: dict[str, str] = field(default_factory=lambda: {"intake-1a2b3c4d": "alpha"})
    decided: dict[str, RecordedDecision] = field(default_factory=dict)
    refusals: list = field(default_factory=list)

    async def workflow_tenant(self, execution_id, workflow_id):
        return self.tenants.get(execution_id)

    async def decision(self, event_id):
        return self.decided.get(event_id)

    async def record_refusal(self, refusal):
        self.refusals.append(refusal)


def _dispatcher(decisions=None, records=None, settings=None, tenant="alpha", bound=2.0):
    return SlackDispatcher(
        tenant=tenant,
        action_key=KEY,
        slack_settings=lambda: _settings() if settings is None else settings,
        decisions=decisions or _Decisions(),
        records=records or _Records(),
        bound_seconds=bound,
    )


ACCEPTED = ParkDecisionOutcome("approved", "slack:E-ORG/U-LEAD", "slack", applied=True)


async def test_an_allowlisted_click_applies_the_decision_to_the_tokens_park():
    decisions = _Decisions(outcome=ACCEPTED)

    result = await _dispatcher(decisions).dispatch(_click())

    assert result.refusal is None
    assert (result.decision, result.actor, result.applied) == (
        "approved",
        "slack:E-ORG/U-LEAD",
        True,
    )
    assert decisions.applied == [
        (
            WORKFLOW,
            "apply_gate_decision",
            (PARK, True, "slack:E-ORG/U-LEAD", "slack"),
            f"slack-decision:alpha:{park_digest(PARK)}",
        )
    ]


async def test_a_repair_click_maps_abandon_to_a_repair_decision():
    decisions = _Decisions(outcome=ParkDecisionOutcome("abandon", "x", "slack", applied=True))
    value = _value(park_key="intake-1a2b3c4d:repair:3:2", kind="repair", actions=("abandon",))

    await _dispatcher(decisions).dispatch(_click(value, action="abandon", user="U-OPS"))

    assert [(update, args[1]) for _, update, args, _ in decisions.applied] == [
        ("apply_repair_decision", False)
    ]


@pytest.mark.parametrize(
    ("click", "refusal"),
    [
        (lambda: _click(_value(tenant="beta")), Refusal.UNVERIFIED),
        (lambda: _click(sign_action_token(_token_for("beta"), b"beta-key")), Refusal.UNVERIFIED),
        (lambda: _click(_value()[:-3] + "AAA"), Refusal.UNVERIFIED),
        (lambda: _click(_value(expires_at=int(time.time()) - 5)), Refusal.EXPIRED),
        (lambda: _click(_value(park_key="")), Refusal.UNVERIFIED),
        (lambda: _click(organization="E-OTHER"), Refusal.WORKSPACE),
        (lambda: _click(channel="C-ELSEWHERE"), Refusal.WORKSPACE),
        (lambda: _click(user="U-OPS"), Refusal.NOT_ALLOWED),
        (lambda: _click(action="reject"), Refusal.NOT_PERMITTED),
    ],
)
async def test_a_click_that_fails_a_check_changes_nothing_and_is_audited(click, refusal):
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records()

    result = await _dispatcher(decisions, records).dispatch(click())

    assert result.refusal is refusal
    assert decisions.applied == []
    assert [entry.reason for entry in records.refusals] == [refusal]
    stored = repr(records.refusals[0])
    assert "v1." not in stored and "beta" not in stored and "beta" not in result.reply


def _token_for(tenant: str) -> ActionToken:
    return ActionToken(tenant, "intake-1a2b3c4d", WORKFLOW, PARK, "gate", ("approve",), 2**40)


async def test_a_token_naming_another_tenants_workflow_reaches_no_workflow():
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records(tenants={"intake-1a2b3c4d": "beta"})

    result = await _dispatcher(decisions, records).dispatch(_click())

    assert result.refusal is Refusal.UNVERIFIED
    assert decisions.applied == []


async def test_a_grid_click_with_no_workspace_team_resolves_with_the_enterprise_actor():
    payload = {
        "team": None,
        "enterprise": {"id": "E-ORG"},
        "user": {"id": "U-LEAD", "team_id": "T-WORKSPACE"},
        "channel": {"id": "C-ESCALATE"},
        "actions": [{"action_id": "approve", "value": _value()}],
    }
    decisions = _Decisions(outcome=ACCEPTED)

    result = await _dispatcher(decisions).dispatch(click_from_payload(payload))

    assert result.refusal is None
    assert decisions.applied[0][2][2] == "slack:E-ORG/U-LEAD"


async def test_a_workspace_click_takes_the_organization_from_the_users_team():
    payload = {
        "user": {"id": "U-LEAD", "team_id": "E-ORG"},
        "channel": {"id": "C-ESCALATE"},
        "actions": [{"action_id": "approve", "value": "v"}],
    }

    assert click_from_payload(payload).organization == "E-ORG"


async def test_a_click_on_an_earlier_park_is_refused_and_names_the_earlier_park():
    decisions = _Decisions(reject=True, waiting="intake-1a2b3c4d:review:security-review:2")
    records = _Records()

    result = await _dispatcher(decisions, records).dispatch(_click())

    assert result.refusal is Refusal.EARLIER_PARK
    assert "earlier" in result.reply
    assert [entry.reason for entry in records.refusals] == [Refusal.EARLIER_PARK]


async def test_a_click_after_the_park_was_decided_reports_the_decision_and_its_actor():
    decided = RecordedDecision(decision="approved", actor="security-lead")
    records = _Records(
        decided={
            "intake-1a2b3c4d:HUMAN_INTERVENTION_DECISION:review:security-review:1": decided,
        }
    )

    result = await _dispatcher(_Decisions(reject=True), records).dispatch(_click())

    assert result.refusal is Refusal.ALREADY_DECIDED
    assert (result.decision, result.actor) == ("approved", "security-lead")
    assert "security-lead" in result.reply


async def test_an_update_that_outlasts_the_bound_reports_the_decision_as_unconfirmed():
    decisions = _Decisions(outcome=ACCEPTED, delay=1.0)

    result = await _dispatcher(decisions, bound=0.05).dispatch(_click())

    assert result.pending
    assert result.refusal is None
    assert "click again" in result.reply.lower()


class _StalledUpdateClient:
    def get_workflow_handle(self, workflow_id):
        return self

    async def execute_update(self, *args, **kwargs):
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError as exc:
            raise WorkflowUpdateRPCTimeoutOrCancelledError() from exc


async def test_a_temporal_update_cancelled_at_the_bound_reports_the_decision_as_unconfirmed():
    dispatcher = _dispatcher(TemporalWorkflowDecisions(_StalledUpdateClient()), bound=0.05)

    result = await dispatcher.dispatch(_click())

    assert result.pending
    assert result.refusal is None


async def test_a_tenant_without_slack_settings_takes_no_decisions():
    decisions = _Decisions(outcome=ACCEPTED)
    dispatcher = SlackDispatcher(
        tenant="alpha",
        action_key=KEY,
        slack_settings=lambda: None,
        decisions=decisions,
        records=_Records(),
    )

    result = await dispatcher.dispatch(_click())

    assert result.refusal is Refusal.DISABLED
    assert decisions.applied == []


async def test_two_clicks_on_one_park_apply_one_update_against_the_workflow():
    from temporalio.testing import WorkflowEnvironment
    from tests.unit.test_draft_pull_request_workflow import (
        SUCCESS,
        _await_park_key,
        _ci_input,
        _make_activities,
        _start_ci_worker,
    )

    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "review")
            records = _Records(tenants={"exec-1": "alpha"})
            dispatcher = _dispatcher(TemporalWorkflowDecisions(env.client), records)
            value = _value(execution_id="exec-1", workflow_id=workflow_id, park_key=park_key)
            first, second = await asyncio.gather(
                dispatcher.dispatch(_click(value)),
                dispatcher.dispatch(_click(value, user="U-OTHER")),
            )
            await handle.result()

    assert first.decision == second.decision == "approved"
    assert first.actor == second.actor
    assert records.refusals == []


async def test_a_click_after_the_run_closed_is_refused_as_not_waiting():
    from temporalio.testing import WorkflowEnvironment
    from tests.unit.test_draft_pull_request_workflow import (
        SUCCESS,
        _await_park_key,
        _ci_input,
        _make_activities,
        _start_ci_worker,
    )

    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "review")
            await handle.signal(
                "submit_gate_decision", args=["security-review", True, "security-lead"]
            )
            await handle.result()
            records = _Records(tenants={"exec-1": "alpha"})
            dispatcher = _dispatcher(TemporalWorkflowDecisions(env.client), records)
            value = _value(execution_id="exec-1", workflow_id=workflow_id, park_key=park_key)
            result = await dispatcher.dispatch(_click(value))

    assert result.refusal is Refusal.NOT_WAITING
    assert [refusal.reason for refusal in records.refusals] == [Refusal.NOT_WAITING]
