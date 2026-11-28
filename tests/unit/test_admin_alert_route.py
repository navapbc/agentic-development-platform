from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from services.integration import credentials
from services.integration.slack.approver_groups import AdminGroupRefresh
from services.integration.slack.dispatcher import (
    SLACK_ADMIN_ACTOR_TYPE,
    AlertOutcome,
    ApproverGroup,
    Refusal,
)
from services.integration.slack.shared_app import SharedSlackApp
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    FIX_DONE,
    MODEL_PATH_ALERT,
    ActionToken,
    alert_ref,
    sign_action_token,
)
from services.orchestration import platform_escalation
from services.orchestration.escalation import (
    REPAIR_KIND,
    SHARED_APP,
    WEBHOOK_TRANSPORT,
    Approvers,
    SlackGroup,
    SlackSettings,
    SlackUser,
)
from services.orchestration.platform_escalation import AdminDestination
from services.orchestration.webhook.server import shared_slack_interactivity, slack_routes
from tests.support import hermetic_settings

KEY = "acme-action-key"
ORG, CHANNEL, ADMIN_CHANNEL = "E0SHARED001", "C0ACME", "C0ADMIN"
APPROVER, ADMIN, OUTSIDER = "U0APPROVER", "U0ADMIN", "U0OUTSIDER"
SHARED = SharedSlackApp("A0SHARED001", ORG, "https://slack.com/api/")
ALERT = alert_ref(MODEL_PATH_ALERT, "episode-1")


def _tenant_slack(app: str) -> SlackSettings:
    return SlackSettings(
        transport=WEBHOOK_TRANSPORT,
        channel=CHANNEL,
        organization_id=ORG,
        app_id="A0ACME" if app != SHARED_APP else SHARED.app_id,
        profile="private",
        content="full",
        allowlist={REPAIR_KIND: Approvers(users=(SlackUser(ORG, APPROVER),))},
        app=app,
    )


ADMINS = AdminDestination(
    slack_channel=ADMIN_CHANNEL,
    allowlist={REPAIR_KIND: Approvers(users=(SlackUser(ORG, ADMIN),))},
    organization_id=ORG,
)


@dataclass
class _Routing:
    def tenants(self) -> list[str]:
        return ["acme"]


@dataclass
class _Records:
    refusals: list = field(default_factory=list)

    async def workflow_tenant(self, execution_id, workflow_id):
        return None

    async def decision(self, event_id):
        return None

    async def record_refusal(self, refusal):
        self.refusals.append(refusal)

    async def approver_groups(self, tenant, group_ids):
        return {}


@dataclass
class _Forwarded:
    requests: list = field(default_factory=list)

    async def act(self, request):
        self.requests.append(request)
        return AlertOutcome(reply="Acknowledged.", applied=True)


def _value(action: str) -> str:
    token = ActionToken(
        tenant="acme",
        execution_id="",
        workflow_id="",
        park_key="",
        kind=REPAIR_KIND,
        actions=(action,),
        expires_at=int(time.time()) + 3600,
        alert=ALERT,
    )
    return sign_action_token(token, KEY.encode())


def _payload(action: str, *, user: str, channel: str, app_id: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "api_app_id": app_id,
        "user": {"id": user, "team_id": ORG},
        "channel": {"id": channel},
        "container": {"message_ts": "200.1"},
        "actions": [{"action_id": action, "value": _value(action)}],
        "response_url": "https://hooks.slack.com/actions/T0/1/x",
    }


def _shared_route(monkeypatch, forwarded, records, admin=lambda: ADMINS):
    monkeypatch.setenv("ESCALATION_ACTION_KEY_ACME", KEY)
    return shared_slack_interactivity(
        hermetic_settings(tenants="acme"),
        _Routing(),
        slack=lambda tenant: _tenant_slack(SHARED_APP) if tenant == "acme" else None,
        channel_tenant=lambda channel: "acme" if channel == CHANNEL else None,
        shared_app=lambda: SHARED,
        decisions=None,
        records=records,
        retained_holder=_no_holder,
        alert_actions=forwarded,
        admin=admin,
    )


async def _no_holder(channel: str) -> None:
    return None


async def test_a_tenant_alert_click_on_the_webhook_route_reaches_the_tenant_worker(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET_ACME", "acme-signing")
    monkeypatch.setenv("ESCALATION_ACTION_KEY_ACME", KEY)
    forwarded = _Forwarded()
    routes = slack_routes(
        hermetic_settings(tenants="acme"),
        _Routing(),
        slack=lambda tenant: _tenant_slack("own"),
        decisions=None,
        records=_Records(),
        alert_actions=forwarded,
    )
    payload = _payload(ACKNOWLEDGE, user=APPROVER, channel=CHANNEL, app_id="A0ACME")
    body = urlencode({"payload": json.dumps(payload)}).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(b"acme-signing", f"v0:{stamp}:".encode() + body, hashlib.sha256)
    headers = {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": f"v0={digest.hexdigest()}"}

    reply = await routes["/webhook/slack/acme"].handle(body, headers)

    [request] = forwarded.requests
    assert (request.tenant, request.action, request.admin) == ("acme", ACKNOWLEDGE, False)
    assert reply.follow_up is not None
    assert reply.follow_up.message.get("response_type") == "ephemeral"


async def test_a_tenant_alert_click_on_the_shared_app_route_reaches_the_tenant_worker(
    monkeypatch,
):
    forwarded = _Forwarded()
    route = _shared_route(monkeypatch, forwarded, _Records())

    reply = await route.decide(
        _payload(ACKNOWLEDGE, user=APPROVER, channel=CHANNEL, app_id=SHARED.app_id)
    )

    [request] = forwarded.requests
    assert (request.channel, request.admin, request.basis) == (CHANNEL, False, "user")
    assert reply.follow_up is not None
    assert reply.follow_up.message["replace_original"] is False


async def test_an_administrator_click_is_admitted_under_the_administrator_allowlist(monkeypatch):
    forwarded, records = _Forwarded(), _Records()
    route = _shared_route(monkeypatch, forwarded, records)

    await route.decide(_payload(FIX_DONE, user=ADMIN, channel=ADMIN_CHANNEL, app_id=SHARED.app_id))

    [request] = forwarded.requests
    assert (request.tenant, request.admin, request.channel) == ("acme", True, ADMIN_CHANNEL)
    assert request.actor_type == SLACK_ADMIN_ACTOR_TYPE
    assert records.refusals == []


@pytest.mark.parametrize("user", [OUTSIDER, APPROVER], ids=["outsider", "tenant-approver"])
async def test_a_click_in_the_administrator_channel_off_the_allowlist_is_refused(monkeypatch, user):
    forwarded, records = _Forwarded(), _Records()
    route = _shared_route(monkeypatch, forwarded, records)

    reply = await route.decide(
        _payload(ACKNOWLEDGE, user=user, channel=ADMIN_CHANNEL, app_id=SHARED.app_id)
    )

    assert forwarded.requests == []
    [refusal] = records.refusals
    assert (refusal.reason, refusal.actor_type) == (Refusal.NOT_ALLOWED, SLACK_ADMIN_ACTOR_TYPE)
    assert reply.follow_up is not None
    assert reply.follow_up.message.get("response_type") == "ephemeral"


async def test_the_administrator_channel_routes_no_click_without_an_administrator_channel(
    monkeypatch,
):
    forwarded, records = _Forwarded(), _Records()
    route = _shared_route(monkeypatch, forwarded, records, admin=lambda: None)

    await route.decide(_payload(FIX_DONE, user=ADMIN, channel=ADMIN_CHANNEL, app_id=SHARED.app_id))

    assert forwarded.requests == []
    assert [refusal.reason for refusal in records.refusals] == [Refusal.UNROUTED]


class _Handle:
    def __init__(self, outcome: AlertOutcome) -> None:
        self._outcome = outcome

    async def result(self) -> AlertOutcome:
        return self._outcome


class _Client:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def start_workflow(self, workflow, request, **options):
        self.started.append({"request": request, **options})
        return _Handle(AlertOutcome(reply="Resolved.", applied=True))


async def test_the_webhook_forwards_an_admitted_click_to_the_tenant_task_queue():
    from services.integration.slack.dispatcher import AlertRequest
    from services.orchestration.alert_actions import ForwardedAlertActions
    from services.orchestration.task_queues import tenant_task_queue

    settings = hermetic_settings(tenants="acme,beta")
    client = _Client()
    request = AlertRequest(
        tenant="beta",
        subject=MODEL_PATH_ALERT,
        alert_id="episode-1",
        action=FIX_DONE,
        actor=f"slack:{ORG}/{ADMIN}",
        user=ADMIN,
        basis="user",
        channel=ADMIN_CHANNEL,
        message_ts="200.1",
        message_blocks=(),
        admin=True,
    )

    outcome = await ForwardedAlertActions(client, settings).act(request)

    [started] = client.started
    assert started["task_queue"] == tenant_task_queue(settings, "beta")
    assert started["request"] is request
    assert outcome.reply == "Resolved."


class _PendingHandle:
    def __init__(self, activity_state) -> None:
        self._activity_state = activity_state

    async def result(self) -> AlertOutcome:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def describe(self):
        from temporalio.client import WorkflowExecutionStatus

        activity = SimpleNamespace(state=self._activity_state)
        return SimpleNamespace(
            status=WorkflowExecutionStatus.RUNNING,
            raw_description=SimpleNamespace(pending_activities=[activity]),
        )


@pytest.mark.parametrize(
    ("state_name", "applied", "reply"),
    [
        ("PENDING_ACTIVITY_STATE_STARTED", True, "is taking this action"),
        ("PENDING_ACTIVITY_STATE_SCHEDULED", False, "has not picked up this action"),
    ],
)
async def test_a_forwarded_click_past_the_bound_says_whether_the_worker_started_it(
    state_name, applied, reply
):
    from services.integration.slack.dispatcher import AlertRequest
    from services.orchestration.alert_actions import ForwardedAlertActions
    from temporalio.api.enums.v1 import PendingActivityState

    class _SlowClient:
        async def start_workflow(self, workflow, request, **options):
            return _PendingHandle(getattr(PendingActivityState, state_name))

    request = AlertRequest(
        tenant="beta",
        subject=MODEL_PATH_ALERT,
        alert_id="episode-1",
        action=FIX_DONE,
        actor=f"slack:{ORG}/{ADMIN}",
        user=ADMIN,
        basis="user",
        channel=ADMIN_CHANNEL,
        message_ts="200.1",
        message_blocks=(),
        admin=True,
    )
    settings = hermetic_settings(tenants="acme,beta")

    outcome = await ForwardedAlertActions(_SlowClient(), settings, bound_seconds=0.01).act(request)

    assert outcome.applied is applied
    assert reply in outcome.reply


ADMIN_GROUP = "S0ADMINGRP1"
GROUP_ADMINS = AdminDestination(
    slack_channel=ADMIN_CHANNEL,
    allowlist={REPAIR_KIND: Approvers(groups=(SlackGroup(ORG, ADMIN_GROUP, ""),))},
    organization_id=ORG,
)


@dataclass
class _GroupStore:
    records: dict = field(default_factory=dict)

    async def record(self, tenant, group_id, members, refreshed_at):
        self.records[(tenant, group_id)] = ApproverGroup(frozenset(members), refreshed_at)

    async def forget_unnamed(self, tenant, named):
        self.records = {k: v for k, v in self.records.items() if k[0] != tenant or k[1] in named}


@dataclass
class _GroupRecords(_Records):
    store: _GroupStore = field(default_factory=_GroupStore)

    async def approver_groups(self, tenant, group_ids):
        return {
            group_id: self.store.records[(tenant, group_id)]
            for group_id in group_ids
            if (tenant, group_id) in self.store.records
        }


@pytest.mark.parametrize(
    ("read_age", "admitted"),
    [(timedelta(minutes=4), True), (timedelta(minutes=11), False)],
    ids=["fresh", "stale"],
)
async def test_an_administrator_group_read_by_the_admin_refresh_admits_only_while_fresh(
    monkeypatch, read_age, admitted
):
    forwarded, records = _Forwarded(), _GroupRecords()
    refresh = AdminGroupRefresh(
        destination=lambda: GROUP_ADMINS,
        read=lambda group: [ADMIN],
        store=records.store,
        clock=lambda: datetime.now(UTC) - read_age,
    )
    await refresh.refresh()
    route = _shared_route(monkeypatch, forwarded, records, admin=lambda: GROUP_ADMINS)

    await route.decide(
        _payload(ACKNOWLEDGE, user=ADMIN, channel=ADMIN_CHANNEL, app_id=SHARED.app_id)
    )

    assert bool(forwarded.requests) is admitted
    expected = [] if admitted else [Refusal.GROUP_STALE]
    assert [refusal.reason for refusal in records.refusals] == expected


def test_the_platform_escalation_path_prefers_the_configured_then_the_local_then_the_platform_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(platform_escalation, "_REPO_ROOT", tmp_path)
    configured, local, platform = (
        tmp_path / "deployed",
        tmp_path / "config" / "local",
        tmp_path / "config" / "platform",
    )
    for directory in (configured, local, platform):
        directory.mkdir(parents=True)
        (directory / platform_escalation.PLATFORM_ESCALATION_FILE).write_text("{}\n")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(configured))

    assert platform_escalation.platform_escalation_path() == configured / "platform-escalation.yaml"
    monkeypatch.delenv("PLATFORM_ENV_CONFIG_DIR")
    assert platform_escalation.platform_escalation_path() == local / "platform-escalation.yaml"
    (local / "platform-escalation.yaml").unlink()
    assert platform_escalation.platform_escalation_path() == platform / "platform-escalation.yaml"
