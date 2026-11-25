from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from services.integration.slack.dispatcher import AlertRequest, Refusal, click_from_payload
from services.integration.slack.messages import (
    AlertContext,
    actions_block,
    limit_request_elements,
)
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    DENY_LIMIT,
    GRANT_LIMIT,
    LIMIT_ALERT,
    LIMIT_REQUEST_ALERT,
    REQUEST_LIMIT,
    LimitRequestRef,
    alert_ref,
    verify_action_token,
)
from services.orchestration import notifications
from services.orchestration.alert_actions import TenantAlertActions
from services.orchestration.limit_grants import (
    DENIED,
    GRANTED,
    LIMIT_INCREASE_DENIED,
    LIMIT_INCREASE_GRANTED,
    LIMIT_INCREASE_REQUESTED,
    PERIOD_SCOPE,
    REQUESTED,
    RUN_SCOPE,
    BudgetPeriod,
    LimitGrant,
    LimitGrantDecisions,
    LimitIncreaseRequest,
    LimitRequests,
    effective_limit,
    request_amounts,
    request_limit_increase,
    tenant_budget_period,
)
from services.orchestration.notifications import SlackPostRef

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
PERIOD = BudgetPeriod(
    start=datetime(2026, 11, 1, tzinfo=UTC), reset=datetime(2026, 12, 1, tzinfo=UTC)
)
ACTION_KEY = "k" * 32
ADMIN = "slack:T1/UADMIN"


class InMemoryLimitGrants:
    def __init__(self, grants: list[LimitGrant] | None = None) -> None:
        self.rows = {grant.request_id: grant for grant in grants or []}

    async def create(self, grant: LimitGrant) -> None:
        self.rows[grant.request_id] = grant

    async def get(self, tenant: str, request_id: str) -> LimitGrant | None:
        grant = self.rows.get(request_id)
        return grant if grant is not None and grant.tenant == tenant else None

    async def decide(self, tenant, request_id, *, state, approver, at):
        grant = await self.get(tenant, request_id)
        if grant is None or grant.state != REQUESTED:
            return None
        self.rows[request_id] = replace(grant, state=state, approver=approver, decided_at=at)
        return self.rows[request_id]

    async def record_post(self, tenant: str, request_id: str, post: SlackPostRef) -> None:
        self.rows[request_id] = replace(
            self.rows[request_id],
            admin_slack_ts=post.ts,
            admin_slack_token=post.owner_token,
            admin_slack_blocks=post.blocks,
        )

    async def for_limit(self, tenant: str, limit_name: str) -> list[LimitGrant]:
        return [g for g in self.rows.values() if g.tenant == tenant and g.limit_name == limit_name]


class RecordingPosts:
    def __init__(self) -> None:
        self.updates: list[tuple[SlackPostRef, str, list[dict[str, Any]]]] = []
        self.replies: list[tuple[SlackPostRef, str]] = []

    async def update(self, post: SlackPostRef, *, text: str, blocks: list[dict[str, Any]]) -> bool:
        self.updates.append((post, text, blocks))
        return True

    async def reply(self, post: SlackPostRef, *, text: str) -> bool:
        self.replies.append((post, text))
        return True


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "tenant": "acme",
        "escalation_action_key": ACTION_KEY,
        "implementation_max_cost_usd": 4.0,
        "implementation_max_tokens": 2_000_000,
        "implementation_max_llm_calls": 200,
        "run_model_budget_usd": 10.0,
        "tenant_model_budget_usd": 100.0,
        "limit_grant_step_percent": 25.0,
        "limit_grant_period_percent": 100.0,
    }
    return SimpleNamespace(**{**values, **overrides})


def _grant(**overrides: Any) -> LimitGrant:
    values: dict[str, Any] = {
        "request_id": "req-1",
        "tenant": "acme",
        "limit_name": "IMPLEMENTATION_MAX_COST_USD",
        "scope": RUN_SCOPE,
        "amount": 1.0,
        "state": GRANTED,
        "requester": "slack:T1/UTENANT",
        "execution_id": "exec-1",
    }
    return LimitGrant(**{**values, **overrides})


def _period_grant(**overrides: Any) -> LimitGrant:
    return _grant(
        limit_name="TENANT_MODEL_BUDGET_USD",
        scope=PERIOD_SCOPE,
        execution_id="",
        period_start=PERIOD.start,
        expires_at=PERIOD.reset,
        **overrides,
    )


def test_the_effective_limit_without_a_grant_is_the_configured_limit():
    assert effective_limit(4.0, [], execution_id="exec-1", now=NOW) == 4.0


def test_a_run_grant_raises_only_its_own_execution():
    grants = [_grant(amount=1.0), _grant(request_id="req-2", amount=0.5, state=REQUESTED)]

    assert effective_limit(4.0, grants, execution_id="exec-1", now=NOW) == 5.0
    assert effective_limit(4.0, grants, execution_id="exec-2", now=NOW) == 4.0


def test_a_period_grant_raises_the_limit_until_the_period_reset():
    grants = [_period_grant(amount=20.0), _period_grant(request_id="req-2", state=DENIED)]

    assert effective_limit(100.0, grants, now=NOW) == 120.0
    assert effective_limit(100.0, grants, now=PERIOD.reset) == 100.0
    assert effective_limit(100.0, grants, now=PERIOD.reset + timedelta(days=1)) == 100.0


def test_a_grant_does_not_turn_an_unlimited_budget_into_a_limit():
    assert effective_limit(0.0, [_period_grant(amount=20.0)], now=NOW) == 0.0


@pytest.mark.parametrize("duration", ["30d", "1mo"])
def test_the_budget_period_ends_at_the_team_budget_reset(duration):
    period = tenant_budget_period({"budget_reset_at": "2026-12-01T00:00:00Z"}, duration)

    assert period == BudgetPeriod(
        start=datetime(2026, 11, 1, tzinfo=UTC), reset=datetime(2026, 12, 1, tzinfo=UTC)
    )


class _Harness:
    def __init__(self, monkeypatch, grants: list[LimitGrant] | None = None, **settings: Any):
        monkeypatch.setattr(notifications, "tenant_slack", lambda settings: None)
        self.settings = _settings(**settings)
        self.store = InMemoryLimitGrants(grants)
        self.audits: list[Any] = []
        self.delivered: list[notifications.AlertMessage] = []

    async def audit(self, event: Any) -> None:
        self.audits.append(event)

    def deliver(self, message: notifications.AlertMessage, settings: Any) -> list[str]:
        self.delivered.append(message)
        message.posts.append(
            SlackPostRef("", "171.1", admin=True, owner_token="owner-1", blocks=json.dumps([]))
        )
        return ["admin_slack"]

    async def request(self, limit_name: str, amount: float, **fields: Any):
        request = LimitIncreaseRequest(
            tenant="acme",
            limit_name=limit_name,
            amount=amount,
            requester="slack:T1/UTENANT",
            usage=fields.pop("usage", 3.9),
            **fields,
        )
        return await request_limit_increase(
            request,
            settings=self.settings,
            store=self.store,
            audit=self.audit,
            deliver=self.deliver,
            now=NOW,
        )


async def test_an_in_band_request_reaches_the_administrators_with_approve_and_deny(monkeypatch):
    harness = _Harness(monkeypatch, [_grant(request_id="old", amount=0.5)])

    outcome = await harness.request("IMPLEMENTATION_MAX_COST_USD", 1.0, execution_id="exec-1")

    assert outcome.accepted
    stored = harness.store.rows[outcome.request_id]
    assert (stored.state, stored.scope, stored.execution_id) == (REQUESTED, RUN_SCOPE, "exec-1")
    assert (stored.admin_slack_ts, stored.admin_slack_token) == ("171.1", "owner-1")
    [message] = harness.delivered
    text = json.dumps(message.slack_blocks)
    for line in (
        "*Tenant:* acme",
        "*Limit:* IMPLEMENTATION_MAX_COST_USD",
        "*Configured:* 4",
        "*Effective:* 4.5",
        "*Usage:* 3.9",
        "*Requested:* 1",
        "*Band:* 1 per request, 4 per period",
        "*Requester:* Slack user UTENANT",
    ):
        assert line in text
    [actions] = [block for block in message.slack_blocks if block["type"] == "actions"]
    assert [button["action_id"] for button in actions["elements"]] == [GRANT_LIMIT, DENY_LIMIT]
    token = verify_action_token(actions["elements"][0]["value"], ACTION_KEY.encode())
    assert token.alert == alert_ref(LIMIT_ALERT, outcome.request_id)
    [audit] = harness.audits
    assert (audit.event_type, audit.decision) == (LIMIT_INCREASE_REQUESTED, REQUESTED)


async def test_a_request_above_the_step_band_is_refused(monkeypatch):
    harness = _Harness(monkeypatch)

    outcome = await harness.request("IMPLEMENTATION_MAX_COST_USD", 1.5, execution_id="exec-1")

    assert not outcome.accepted
    assert "step band of 1" in outcome.reply
    assert "The configured limit IMPLEMENTATION_MAX_COST_USD must change." in outcome.reply
    assert harness.store.rows == {} and harness.delivered == []
    [audit] = harness.audits
    assert (audit.event_type, audit.decision) == (LIMIT_INCREASE_REQUESTED, "refused")


async def test_a_request_above_the_run_period_band_is_refused(monkeypatch):
    granted = [_grant(request_id=f"r{n}", amount=1.0) for n in range(4)]
    harness = _Harness(monkeypatch, [*granted, _grant(request_id="other", execution_id="exec-2")])

    within = await harness.request("IMPLEMENTATION_MAX_COST_USD", 1.0, execution_id="exec-2")
    over = await harness.request("IMPLEMENTATION_MAX_COST_USD", 1.0, execution_id="exec-1")

    assert within.accepted
    assert not over.accepted
    assert "period band of 4" in over.reply and "must change" in over.reply


async def test_a_request_above_the_budget_period_band_is_refused(monkeypatch):
    earlier = replace(
        _period_grant(request_id="earlier", amount=90.0),
        period_start=PERIOD.start - timedelta(days=30),
        expires_at=PERIOD.start,
    )
    harness = _Harness(monkeypatch, [earlier, _period_grant(request_id="this", amount=80.0)])

    within = await harness.request("TENANT_MODEL_BUDGET_USD", 20.0, period=PERIOD)
    over = await harness.request("TENANT_MODEL_BUDGET_USD", 25.0, period=PERIOD)

    assert within.accepted
    assert harness.store.rows[within.request_id].expires_at == PERIOD.reset
    assert not over.accepted
    assert "period band of 100" in over.reply


def _click(request_id: str, action: str, *, admin: bool = True) -> AlertRequest:
    blocks = (
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Limit increase requested*"}},
        {"type": "actions", "block_id": "adp-actions", "elements": []},
    )
    return AlertRequest(
        tenant="acme",
        subject=LIMIT_ALERT,
        alert_id=request_id,
        action=action,
        actor=ADMIN,
        user="UADMIN",
        basis="user",
        channel="CADMIN",
        message_ts="171.1",
        message_blocks=blocks,
        admin=admin,
    )


def _decisions(store: InMemoryLimitGrants, audits: list[Any], posts: RecordingPosts):
    async def audit(event: Any) -> None:
        audits.append(event)

    return LimitGrantDecisions(store=store, audit=audit, posts=posts, clock=lambda: NOW)


@pytest.mark.parametrize(
    ("action", "state", "event", "line"),
    [
        (GRANT_LIMIT, GRANTED, LIMIT_INCREASE_GRANTED, "Granted by <@UADMIN>"),
        (DENY_LIMIT, DENIED, LIMIT_INCREASE_DENIED, "Denied by <@UADMIN>"),
    ],
)
async def test_an_administrator_decision_is_recorded_audited_and_shown_on_the_post(
    action, state, event, line
):
    store = InMemoryLimitGrants(
        [_grant(state=REQUESTED, admin_slack_ts="171.1", admin_slack_token="owner-1")]
    )
    audits: list[Any] = []
    posts = RecordingPosts()

    outcome = await _decisions(store, audits, posts).act(_click("req-1", action))
    again = await _decisions(store, audits, posts).act(_click("req-1", GRANT_LIMIT))

    assert outcome.applied
    assert (store.rows["req-1"].state, store.rows["req-1"].approver) == (state, ADMIN)
    [audit] = audits
    assert (audit.event_type, audit.actor_id, audit.actor_type) == (event, ADMIN, "slack_admin")
    [(post, _, blocks)] = posts.updates
    assert (post.ts, post.admin, post.owner_token) == ("171.1", True, "owner-1")
    assert not any(block["type"] == "actions" for block in blocks)
    assert line in json.dumps(blocks)
    assert again.refusal is Refusal.ALREADY_DECIDED
    assert effective_limit(4.0, list(store.rows.values()), execution_id="exec-1", now=NOW) == (
        5.0 if state == GRANTED else 4.0
    )


async def test_a_limit_click_from_outside_the_administrator_route_is_refused():
    store = InMemoryLimitGrants([_grant(state=REQUESTED)])
    audits: list[Any] = []
    posts = RecordingPosts()
    actions = TenantAlertActions(
        settings=SimpleNamespace(tenant="acme"),
        gate=None,
        episodes=None,
        ledger=None,
        audit=None,
        posts=posts,
        restart=None,
        validate=None,
        trigger_intake=None,
        limits=_decisions(store, audits, posts),
    )

    outcome = await actions.act(_click("req-1", GRANT_LIMIT, admin=False))

    assert outcome.refusal is Refusal.NOT_ALLOWED
    assert store.rows["req-1"].state == REQUESTED
    assert audits == [] and posts.updates == []


@pytest.mark.parametrize(("action", "resumes"), [(GRANT_LIMIT, True), (DENY_LIMIT, False)])
async def test_a_grant_resumes_the_parked_run_and_a_deny_leaves_it_parked(action, resumes):
    store = InMemoryLimitGrants([_grant(state=REQUESTED)])
    resumed: list[Any] = []

    async def resume(grant: Any) -> list[str]:
        resumed.append(grant)
        return ["wf-1"]

    async def audit(event: Any) -> None:
        return None

    decisions = LimitGrantDecisions(
        store=store, audit=audit, posts=RecordingPosts(), resume=resume, clock=lambda: NOW
    )
    outcome = await decisions.act(_click("req-1", action))

    assert [(grant.request_id, grant.approver) for grant in resumed] == (
        [("req-1", ADMIN)] if resumes else []
    )
    assert ("Resumed wf-1." in outcome.reply) is resumes


PARK_KEY = "exec-1:repair:IMPLEMENTATION_MAX_COST_USD:2"
LIMIT_REF = LimitRequestRef(usage=4.1, park_key=PARK_KEY)


def _limit_post(amounts: tuple[float, ...]) -> list[dict[str, Any]]:
    context = AlertContext(
        tenant="acme",
        alert=alert_ref(LIMIT_REQUEST_ALERT, LIMIT_REF.alert_id),
        issued_at=int(NOW.timestamp()),
        ttl_hours=72,
        action_key=ACTION_KEY.encode(),
        execution_id="exec-1",
        workflow_id="wf-1",
    )
    abandon = {"type": "button", "action_id": "abandon", "value": "park-token"}
    elements = [*limit_request_elements(context, amounts), abandon]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "parked"}},
        *actions_block(elements),
    ]


def _select_click(blocks: list[dict[str, Any]], amount: str) -> AlertRequest:
    payload = {
        "user": {"id": "UTENANT", "team_id": "T1"},
        "channel": {"id": "CTENANT"},
        "container": {"message_ts": "170.1"},
        "message": {"ts": "170.1", "blocks": blocks},
        "actions": [
            {
                "type": "static_select",
                "action_id": REQUEST_LIMIT,
                "block_id": "adp-actions",
                "selected_option": {"value": amount},
            }
        ],
    }
    click = click_from_payload(payload)
    token = verify_action_token(click.value, ACTION_KEY.encode(), now=NOW.timestamp())
    assert click.action in token.actions
    return AlertRequest(
        tenant=token.tenant,
        subject=token.alert_subject,
        alert_id=token.alert_id,
        action=click.action,
        actor=click.actor,
        user=click.user,
        basis="channel",
        channel=click.channel,
        message_ts=click.message_ts,
        message_blocks=click.message_blocks,
        execution_id=token.execution_id,
        workflow_id=token.workflow_id,
        option=click.option,
    )


@pytest.mark.parametrize(("granted", "offered"), [(0.0, (0.4, 1.0)), (3.5, (0.4, 0.5)), (4.0, ())])
async def test_request_increase_offers_only_amounts_inside_the_band(monkeypatch, granted, offered):
    prior = [_grant(request_id="old", amount=granted)] if granted else []
    harness = _Harness(monkeypatch, prior)

    amounts = await request_amounts(
        harness.settings, harness.store, "IMPLEMENTATION_MAX_COST_USD", execution_id="exec-1"
    )
    [*_, actions] = _limit_post(amounts) if amounts else [{}]

    assert amounts == offered
    if amounts:
        select, acknowledge, _ = actions["elements"]
        assert [option["value"] for option in select["options"]] == [f"{a:g}" for a in offered]
        token = verify_action_token(acknowledge["value"], ACTION_KEY.encode(), now=NOW.timestamp())
        assert (acknowledge["action_id"], token.actions) == (
            ACKNOWLEDGE,
            (ACKNOWLEDGE, REQUEST_LIMIT),
        )
    for amount in offered:
        assert (
            await harness.request("IMPLEMENTATION_MAX_COST_USD", amount, execution_id="exec-1")
        ).accepted


async def test_a_forged_request_amount_outside_the_band_is_refused(monkeypatch):
    harness = _Harness(monkeypatch)
    requests = LimitRequests(
        settings=harness.settings, store=harness.store, audit=harness.audit, deliver=harness.deliver
    )
    blocks = _limit_post((0.4, 1.0))

    forged = await requests.act(_select_click(blocks, "50"))
    chosen = await requests.act(_select_click(blocks, "1"))

    assert not forged.applied
    assert "The configured limit IMPLEMENTATION_MAX_COST_USD must change." in forged.reply
    assert chosen.applied and len(harness.delivered) == 1
    [row] = harness.store.rows.values()
    assert (row.amount, row.execution_id, row.requester) == (1.0, "exec-1", "slack:T1/UTENANT")
    assert (row.tenant_slack_channel, row.tenant_slack_ts) == ("CTENANT", "170.1")
    refused, requested = (event.decision for event in harness.audits)
    assert (refused, requested) == ("refused", REQUESTED)


def _requested_on_tenant_post() -> LimitGrant:
    return _grant(
        state=REQUESTED,
        admin_slack_ts="171.1",
        admin_slack_token="owner-1",
        tenant_slack_channel="CTENANT",
        tenant_slack_ts="170.1",
        tenant_slack_blocks=json.dumps(_limit_post((0.4, 1.0))),
    )


async def test_a_grant_edits_the_tenant_post_replies_in_its_thread_and_resumes_the_run():
    store = InMemoryLimitGrants([_requested_on_tenant_post()])
    posts = RecordingPosts()
    resumed: list[str] = []

    async def resume(grant: LimitGrant) -> list[str]:
        resumed.append(grant.execution_id)
        return ["wf-1"]

    async def audit(event: Any) -> None:
        return None

    decisions = LimitGrantDecisions(
        store=store, audit=audit, posts=posts, resume=resume, clock=lambda: NOW
    )
    await decisions.act(_click("req-1", GRANT_LIMIT))

    admin, tenant = posts.updates
    assert (admin[0].ts, tenant[0].ts, tenant[0].admin) == ("171.1", "170.1", False)
    assert not any(block["type"] == "actions" for block in tenant[2])
    assert "Granted by <@UADMIN>" in json.dumps(tenant[2])
    [(post, text)] = posts.replies
    assert (post.channel, post.ts) == ("CTENANT", "170.1")
    assert (
        text
        == "Granted 1 more of IMPLEMENTATION_MAX_COST_USD by Slack user UADMIN. The run resumes."
    )
    assert resumed == ["exec-1"]


async def test_a_deny_keeps_abandon_on_the_tenant_post_and_tells_the_tenant_in_its_thread():
    store = InMemoryLimitGrants([_requested_on_tenant_post()])
    posts = RecordingPosts()
    audits: list[Any] = []

    await _decisions(store, audits, posts).act(_click("req-1", DENY_LIMIT))

    _, (post, _, blocks) = posts.updates
    [actions] = [block for block in blocks if block["type"] == "actions"]
    assert [element["action_id"] for element in actions["elements"]] == ["abandon"]
    assert "Denied by <@UADMIN>" in json.dumps(blocks)
    assert blocks.index(actions) == len(blocks) - 1
    [(replied, text)] = posts.replies
    assert replied == post
    assert "The run stays parked. Press Abandon to end it." in text
