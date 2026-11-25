from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from services.integration.slack.dispatcher import (
    GROUP_STALE_SECONDS,
    REFUSAL_EVENT,
    ApproverGroup,
    DecisionRejected,
    PostgresDecisionRecords,
    RecordedDecision,
    Refusal,
    SlackClick,
    SlackDispatcher,
    TemporalWorkflowDecisions,
    click_from_payload,
    refuse_unrouted,
)
from services.integration.slack.tokens import ActionToken, park_digest, sign_action_token
from services.orchestration.escalation import Approvers, SlackGroup, SlackSettings, SlackUser
from services.orchestration.temporal_workflows.draft_pull_request import (
    DraftPullRequestWorkflow,
    ParkDecisionOutcome,
)
from services.orchestration.webhook.slack_events import follow_up_message
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
            "gate": Approvers(users=(SlackUser("E-ORG", "U-LEAD"), SlackUser("E-ORG", "U-OTHER"))),
            "repair": Approvers(users=(SlackUser("E-ORG", "U-OPS"),)),
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
    refreshed: list[tuple] = field(default_factory=list)

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

    async def refresh(self, workflow_id, park_key, update_id):
        self.refreshed.append((workflow_id, park_key, update_id))
        if self.reject:
            raise DecisionRejected("stale")
        return True


@dataclass
class _Records:
    tenants: dict[str, str] = field(default_factory=lambda: {"intake-1a2b3c4d": "alpha"})
    decided: dict[str, RecordedDecision] = field(default_factory=dict)
    refusals: list = field(default_factory=list)
    groups: dict[tuple[str, str], ApproverGroup] = field(default_factory=dict)
    group_reads: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    async def workflow_tenant(self, execution_id, workflow_id):
        return self.tenants.get(execution_id)

    async def decision(self, event_id):
        return self.decided.get(event_id)

    async def record_refusal(self, refusal):
        self.refusals.append(refusal)

    async def approver_groups(self, tenant, group_ids):
        self.group_reads.append((tenant, tuple(group_ids)))
        return {
            group_id: self.groups[(tenant, group_id)]
            for group_id in group_ids
            if (tenant, group_id) in self.groups
        }


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
            (PARK, True, "slack:E-ORG/U-LEAD", "slack", "user"),
            f"slack-decision:alpha:{park_digest(PARK)}",
        )
    ]


async def test_a_click_from_the_retained_channel_of_the_tenant_applies_its_decision():
    decisions = _Decisions(outcome=ACCEPTED)
    dispatcher = _dispatcher(decisions)

    retained = await dispatcher.dispatch(_click(channel="C-LEFT"), retained_channel="C-LEFT")
    elsewhere = await dispatcher.dispatch(_click(channel="C-OTHER"), retained_channel="C-LEFT")

    assert retained.refusal is None and retained.actor == "slack:E-ORG/U-LEAD"
    assert elsewhere.refusal is Refusal.WORKSPACE


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
    assert _names_no_command(result.reply)


def _names_no_command(reply: str) -> bool:
    return "command" not in reply.lower() and "Next step" not in reply


def _token_for(tenant: str) -> ActionToken:
    return ActionToken(tenant, "intake-1a2b3c4d", WORKFLOW, PARK, "gate", ("approve",), 2**40)


GROUP = "S0123456789"
REPAIR_PARK = "intake-1a2b3c4d:repair:3:2"


def _group(*members: str, age_seconds: float = 0) -> ApproverGroup:
    return ApproverGroup(
        members=frozenset(members),
        refreshed_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


def _repair_click(user: str) -> SlackClick:
    value = _value(park_key=REPAIR_PARK, kind="repair", actions=("retry",))
    return _click(value, action="retry", user=user)


def _repair_settings(*approvers: SlackUser | SlackGroup) -> SlackSettings:
    users = tuple(entry for entry in approvers if isinstance(entry, SlackUser))
    groups = tuple(entry for entry in approvers if isinstance(entry, SlackGroup))
    return _settings(allowlist={"repair": Approvers(users=users, groups=groups)})


async def test_a_kind_in_channel_mode_admits_anyone_of_the_organization_in_the_channel():
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records()
    settings = _settings(allowlist={"gate": Approvers(anyone_in_channel=True)})
    dispatcher = _dispatcher(decisions, records, settings)

    admitted = await dispatcher.dispatch(_click(user="U-ANYONE"))
    connect = await dispatcher.dispatch(_click(user="U-ANYONE", organization="E-PARTNER"))

    assert admitted.refusal is None
    assert decisions.applied[0][2] == (PARK, True, "slack:E-ORG/U-ANYONE", "slack", "channel")
    assert connect.refusal is Refusal.WORKSPACE
    assert len(decisions.applied) == 1
    assert records.group_reads == []


async def test_a_named_group_admits_a_fresh_member_and_refuses_a_stale_one_as_stale():
    decisions = _Decisions(outcome=ParkDecisionOutcome("retry", "x", "slack", applied=True))
    records = _Records(groups={("alpha", GROUP): _group("U-MEMBER")})
    dispatcher = _dispatcher(decisions, records, _repair_settings(SlackGroup("E-ORG", GROUP)))

    member = await dispatcher.dispatch(_repair_click("U-MEMBER"))
    outsider = await dispatcher.dispatch(_repair_click("U-OUTSIDER"))
    records.groups[("alpha", GROUP)] = _group("U-MEMBER", age_seconds=GROUP_STALE_SECONDS + 1)
    stale = await dispatcher.dispatch(_repair_click("U-MEMBER"))
    records.groups[("alpha", GROUP)] = _group("U-MEMBER")
    refreshed = await dispatcher.dispatch(_repair_click("U-MEMBER"))

    assert member.refusal is None and refreshed.refusal is None
    assert [args[4] for _, update, args, _ in decisions.applied] == [f"group:{GROUP}"] * 2
    assert {update for _, update, _, _ in decisions.applied} == {"apply_repair_decision"}
    assert outsider.refusal is Refusal.NOT_ALLOWED
    assert stale.refusal is Refusal.GROUP_STALE
    assert "ten minutes" in stale.reply
    assert _names_no_command(stale.reply)
    assert [entry.reason for entry in records.refusals] == [
        Refusal.NOT_ALLOWED,
        Refusal.GROUP_STALE,
    ]


async def test_a_mixed_list_admits_a_named_user_whose_group_record_is_stale():
    decisions = _Decisions(outcome=ParkDecisionOutcome("retry", "x", "slack", applied=True))
    records = _Records(groups={("alpha", GROUP): _group("U-OPS", age_seconds=3600)})
    settings = _repair_settings(SlackUser("E-ORG", "U-OPS"), SlackGroup("E-ORG", GROUP))

    result = await _dispatcher(decisions, records, settings).dispatch(_repair_click("U-OPS"))

    assert result.refusal is None
    assert decisions.applied[0][2][4] == "user"
    assert records.group_reads == []


async def test_another_tenants_record_of_a_group_with_the_same_id_is_not_read():
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records(groups={("beta", GROUP): _group("U-MEMBER")})
    dispatcher = _dispatcher(decisions, records, _repair_settings(SlackGroup("E-ORG", GROUP)))

    result = await dispatcher.dispatch(_repair_click("U-MEMBER"))

    assert result.refusal is Refusal.NOT_ALLOWED
    assert records.group_reads == [("alpha", (GROUP,))]
    assert decisions.applied == []


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


class _Transaction:
    def __init__(self) -> None:
        self.statements: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)


class _Engine:
    def __init__(self) -> None:
        self.transaction = _Transaction()

    def begin(self):
        return self.transaction


async def test_a_click_on_a_channel_no_tenant_names_is_refused_with_its_route_and_channel():
    engine = _Engine()

    result = await refuse_unrouted(
        _click(channel="C3"), PostgresDecisionRecords(engine), route="/webhook/slack/_shared"
    )

    assert result.refusal is Refusal.UNROUTED
    assert not result.accepted
    assert _names_no_command(result.reply)
    [statement] = engine.transaction.statements
    values = statement.compile().params
    assert values["event_type"] == REFUSAL_EVENT
    assert values["reason"] == Refusal.UNROUTED.value
    assert values["correlation_ids"] == {
        "tenant": "",
        "park_digest": "",
        "route": "/webhook/slack/_shared",
        "channel": "C3",
    }


class _Rows:
    def all(self):
        return []


class _Connection(_Transaction):
    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows()


async def test_the_postgres_group_read_selects_only_the_named_groups_of_the_given_tenant():
    connection = _Connection()
    engine = _Engine()
    engine.connect = lambda: connection

    assert await PostgresDecisionRecords(engine).approver_groups("alpha", [GROUP]) == {}

    [statement] = connection.statements
    assert sorted(statement.compile().params.values(), key=str) == [[GROUP], "alpha"]


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


PARK_POST = (
    {"type": "section", "text": {"type": "mrkdwn", "text": "*ADSER-24* parked at security-review"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Reason:* review_blocked"}},
    {"type": "actions", "block_id": "adp-actions", "elements": [{"type": "button"}]},
)


@pytest.mark.parametrize(
    ("kind", "park", "action", "decision", "user", "label"),
    [
        ("gate", PARK, "approve", "approved", "U-LEAD", "Approved"),
        ("gate", PARK, "reject", "rejected", "U-LEAD", "Rejected"),
        ("repair", REPAIR_PARK, "retry", "retry", "U-OPS", "Retry requested"),
        ("repair", REPAIR_PARK, "abandon", "abandon", "U-OPS", "Abandoned"),
    ],
)
async def test_a_decided_post_keeps_its_body_and_replaces_its_buttons_with_a_mention_line(
    kind, park, action, decision, user, label
):
    actor = f"slack:E-ORG/{user}"
    decisions = _Decisions(outcome=ParkDecisionOutcome(decision, actor, "slack", applied=True))
    value = _value(park_key=park, kind=kind, actions=(action,))
    click = _click(value, action=action, user=user, message_blocks=PARK_POST)

    message = follow_up_message(await _dispatcher(decisions).dispatch(click))

    blocks = message["blocks"]
    assert message["replace_original"] is True
    assert blocks[:2] == list(PARK_POST[:2])
    assert not any(block["type"] == "actions" for block in blocks)
    assert blocks[-1]["elements"][0]["text"].startswith(f"{label} by <@{user}> at ")
    assert "slack:" not in json.dumps(blocks) and "&lt;@" not in json.dumps(blocks)


async def test_a_decided_post_without_message_blocks_renders_its_title_and_the_status_line():
    click = _click(message_text="ADSER-24 parked <now>")

    message = follow_up_message(await _dispatcher(_Decisions(outcome=ACCEPTED)).dispatch(click))

    title, line = message["blocks"]
    assert title["text"]["text"] == "*ADSER-24 parked &lt;now&gt;*"
    assert line["elements"][0]["text"].startswith("Approved by <@U-LEAD> at ")


def _expired_value(**overrides) -> str:
    return _value(expires_at=int(time.time()) - 5, **overrides)


async def test_an_approvers_expired_clicks_ask_the_run_once_a_minute_for_fresh_buttons(
    monkeypatch,
):
    from services.integration.slack import dispatcher as dispatcher_module

    monkeypatch.setattr(dispatcher_module.time, "time", lambda: 1_700_000_030.0)
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records()
    value = sign_action_token(
        ActionToken("alpha", "intake-1a2b3c4d", WORKFLOW, PARK, "gate", ("approve",), 1_000), KEY
    )

    results = [await _dispatcher(decisions, records).dispatch(_click(value)) for _ in range(3)]

    assert [result.reply for result in results] == [
        "These buttons expired. Fresh buttons are posted in this message's thread."
    ] * 3
    assert {result.refusal for result in results} == {Refusal.EXPIRED}
    assert decisions.applied == []
    assert len({update_id for _, _, update_id in decisions.refreshed}) == 1
    assert {(workflow, park) for workflow, park, _ in decisions.refreshed} == {(WORKFLOW, PARK)}
    assert {entry.park_digest for entry in records.refusals} == {park_digest(PARK)}


@pytest.mark.parametrize(
    "click",
    [
        lambda: _click(_expired_value(), user="U-OPS"),
        lambda: _click(_expired_value(), channel="C-ELSEWHERE"),
        lambda: _click(_expired_value(tenant="beta")),
    ],
)
async def test_an_expired_click_the_park_would_not_admit_is_refused_and_refreshes_nothing(click):
    decisions = _Decisions(outcome=ACCEPTED)
    records = _Records()

    result = await _dispatcher(decisions, records).dispatch(click())

    assert result.refusal is Refusal.EXPIRED
    assert result.reply == "This action has expired, so it was not applied."
    assert decisions.refreshed == []
    assert [entry.reason for entry in records.refusals] == [Refusal.EXPIRED]


async def test_an_expired_click_on_a_decided_park_gets_the_decided_refusal():
    token = ActionToken("alpha", "intake-1a2b3c4d", WORKFLOW, PARK, "gate", ("approve",), 0)
    decisions = _Decisions(reject=True)
    records = _Records(
        decided={token.decision_event_id: RecordedDecision("approved", "slack:E-ORG/U-OTHER")}
    )

    result = await _dispatcher(decisions, records).dispatch(_click(_expired_value()))

    assert result.refusal is Refusal.ALREADY_DECIDED
    assert len(decisions.refreshed) == 1
