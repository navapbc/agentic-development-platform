from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.slack.dispatcher import (
    SLACK_ADMIN_ACTOR_TYPE,
    Refusal,
    SlackClick,
    SlackDispatcher,
)
from services.integration.slack.messages import (
    ACKNOWLEDGED_BLOCK,
    RESOLVED_BLOCK,
    AlertContext,
    PostState,
    SlackAlert,
    alert_payload,
    labeled_blocks,
)
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    FIX_DONE,
    MODEL_PATH_ALERT,
    RESTART,
    RUN_ALERT,
    VALIDATE,
    ActionToken,
    alert_ref,
    sign_action_token,
)
from services.orchestration import remedies
from services.orchestration.alert_actions import RestartOutcome, TenantAlertActions
from services.orchestration.escalation import REPAIR_KIND
from services.orchestration.model_path import OutagePost
from tests.support import (
    InMemoryModelPathEpisodes,
    ModelPathStub,
    capture_alerts,
    hermetic_settings,
    model_path_gate,
)

KEY = b"action-key"
ORG, CHANNEL, APPROVER = "T0123456789", "C0123456789", "U0APPROVER"


@dataclass
class _Approvers:
    users: frozenset[str]
    anyone_in_channel: bool = False

    def names_user(self, organization: str, user: str) -> bool:
        return organization == ORG and user in self.users

    def group_ids(self, organization: str) -> tuple[str, ...]:
        return ()


@dataclass
class _Slack:
    channel: str = CHANNEL
    organization_id: str = ORG

    def approvers(self, kind: str) -> _Approvers:
        assert kind == REPAIR_KIND
        return _Approvers(frozenset({APPROVER}))


@dataclass
class _Records:
    refusals: list = field(default_factory=list)

    async def workflow_tenant(self, execution_id, workflow_id):
        return "acme" if (execution_id, workflow_id) == ("exec-1", "wf-1") else None

    async def decision(self, event_id):
        return None

    async def record_refusal(self, refusal):
        self.refusals.append(refusal.reason)

    async def approver_groups(self, tenant, group_ids):
        return {}


@dataclass
class _Posts:
    updates: list = field(default_factory=list)
    replies: list = field(default_factory=list)

    async def update(self, post, *, text, blocks):
        self.updates.append((post.ts, blocks))
        return True

    async def reply(self, post, *, text):
        self.replies.append((post.ts, text))
        return True


class _World:
    def __init__(self, monkeypatch, *, hop: str = "bedrock_path", **settings) -> None:
        self.settings = hermetic_settings(tenant="acme", **settings)
        self.alerts = capture_alerts(monkeypatch)
        self.edits: list = []
        self.edited_posts: list = []
        monkeypatch.setattr(
            "services.orchestration.notifications.update_slack_post",
            lambda post, **kwargs: (
                (self.edited_posts.append(post) or self.edits.append((post.ts, kwargs["blocks"])))
                or True
            ),
        )
        self.path = ModelPathStub(hop)
        self.episodes = InMemoryModelPathEpisodes()
        self.ledger = InMemorySideEffectLedger()
        self.gate = model_path_gate(
            self.path, ledger=self.ledger, settings=self.settings, episodes=self.episodes
        )
        self.posts, self.records, self.audits = _Posts(), _Records(), []
        self.restarts: list[str] = []
        self.intake_triggers = 0
        self.dispatcher = SlackDispatcher(
            tenant="acme",
            action_key=KEY,
            slack_settings=_Slack,
            decisions=None,
            records=self.records,
            alert_actions=TenantAlertActions(
                settings=self.settings,
                gate=self.gate,
                episodes=self.episodes,
                ledger=self.ledger,
                audit=self._audit,
                posts=self.posts,
                restart=self._restart,
                validate=self._validate,
                trigger_intake=self._trigger,
                validate_platform=self._validate_platform,
            ),
        )

    async def _audit(self, event):
        self.audits.append(event)

    async def _restart(self, execution_id, actor):
        self.restarts.append(execution_id)
        await asyncio.sleep(0)
        return RestartOutcome(True, f"T-1 restarted by {actor}")

    async def _validate(self):
        return "Deployment validation of tenant acme\nFAIL model_path: the Bedrock connection fails"

    async def _trigger(self):
        self.intake_triggers += 1

    async def _validate_platform(self):
        return "Deployment validation of the platform\nFAIL model_aliases: the alias is missing"

    async def open_outage(self, blocks: str = "") -> str:
        await self.gate.admit(issue_key="T-1")
        episode = self.episodes.open_episodes["acme"].episode
        await self.episodes.record_post(
            "acme", episode, OutagePost(CHANNEL, "100.1", blocks=blocks)
        )
        return episode

    def admin_click(self, alert, action, *, user="U0ADMIN"):
        token = ActionToken(
            tenant="acme",
            execution_id="",
            workflow_id="",
            park_key="",
            kind=REPAIR_KIND,
            actions=(action,),
            expires_at=int(time.time()) + 3600,
            alert=alert,
        )
        click = SlackClick(
            value=sign_action_token(token, KEY),
            action=action,
            organization=ORG,
            user=user,
            channel="C0ADMIN",
            message_ts="200.1",
            message_blocks=({"type": "section", "text": {"type": "mrkdwn", "text": "x"}},),
        )
        return self.dispatcher.dispatch_admin(
            click,
            channel="C0ADMIN",
            organization=ORG,
            approvers=_Approvers(frozenset({"U0ADMIN"})),
        )

    def click(
        self, alert, action, *, user=APPROVER, ts="100.1", execution="", workflow="", blocks=None
    ):
        token = ActionToken(
            tenant="acme",
            execution_id=execution,
            workflow_id=workflow,
            park_key="",
            kind=REPAIR_KIND,
            actions=(action,),
            expires_at=int(time.time()) + 3600,
            alert=alert,
        )
        return self.dispatcher.dispatch(
            SlackClick(
                value=sign_action_token(token, KEY),
                action=action,
                organization=ORG,
                user=user,
                channel=CHANNEL,
                message_ts=ts,
                message_blocks=tuple(blocks)
                if blocks is not None
                else ({"type": "section", "text": {"type": "mrkdwn", "text": "x"}},),
            )
        )


OUTAGE_LINES = [("What happened", "The Bedrock connection fails."), ("Effect", "Intake waits.")]


def _outage_blocks() -> list[dict]:
    button = {"type": "button", "action_id": FIX_DONE, "text": {"type": "plain_text", "text": "x"}}
    return labeled_blocks("Model path unavailable", OUTAGE_LINES, [button])


def _texts(blocks) -> str:
    return json.dumps(blocks)


def _context_text(blocks, block_id: str) -> str:
    [block] = [block for block in blocks if block.get("block_id") == block_id]
    return block["elements"][0]["text"]


async def test_ae18_acknowledge_validate_and_fix_done_resolve_the_outage(monkeypatch):
    world = _World(monkeypatch)
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage())

    refused = await world.click(alert, ACKNOWLEDGE, user="U0OUTSIDER")
    acknowledged = await world.click(alert, ACKNOWLEDGE)
    await world.click(alert, VALIDATE)
    still_failing = await world.click(alert, FIX_DONE)
    world.path.hop = ""
    fixed = await world.click(alert, FIX_DONE)

    assert refused.refusal is Refusal.NOT_ALLOWED
    assert acknowledged.accepted and APPROVER in acknowledged.reply
    episode = world.episodes.closed[0]
    assert episode.acknowledged_by == f"slack:{ORG}/{APPROVER}"
    [(ts, blocks)] = world.posts.updates
    assert ts == "100.1"
    assert any(block.get("block_id") == ACKNOWLEDGED_BLOCK for block in blocks)
    assert world.posts.replies == [("100.1", await world._validate())]
    assert "bedrock_path" in still_failing.reply and not still_failing.applied
    assert fixed.applied
    assert world.episodes.resolvers == [f"slack:{ORG}/{APPROVER}"]
    assert world.intake_triggers == 1
    assert world.alerts[-1].subject == "Model path recovered for tenant acme"
    assert world.alerts[-1].thread_ts == "100.1"
    assert [ts for ts, _ in world.edits] == ["100.1"]
    assert [event.decision for event in world.audits] == [
        "acknowledged",
        "validated",
        "still_failing",
        "resolved",
    ]
    assert {event.correlation_ids["basis"] for event in world.audits} == {"user"}


async def test_a_click_on_a_resolved_outage_or_an_earlier_post_is_refused(monkeypatch):
    world = _World(monkeypatch)
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage())

    earlier = await world.click(alert, ACKNOWLEDGE, ts="99.9")
    stale = await world.click(alert_ref(MODEL_PATH_ALERT, "episode-0"), ACKNOWLEDGE)

    assert earlier.refusal is Refusal.EARLIER_POST
    assert stale.refusal is Refusal.RESOLVED
    assert world.records.refusals == [Refusal.EARLIER_POST, Refusal.RESOLVED]
    assert world.posts.updates == [] and world.audits == []


async def test_restart_run_restarts_the_run_once_under_double_clicks(monkeypatch):
    world = _World(monkeypatch)
    run = alert_ref(RUN_ALERT, "exec-1")

    def restart():
        return world.click(run, RESTART, execution="exec-1", workflow="wf-1")

    concurrent = await asyncio.gather(restart(), restart())
    later = await restart()

    assert world.restarts == ["exec-1"]
    assert sorted(str(result.refusal) for result in concurrent) == [
        "None",
        str(Refusal.ALREADY_RESTARTED),
    ]
    assert later.refusal is Refusal.ALREADY_RESTARTED
    assert [event.decision for event in world.audits] == ["restarted"]
    assert len(world.posts.updates) == 1


async def test_a_run_alert_for_another_tenant_s_execution_is_refused(monkeypatch):
    world = _World(monkeypatch)

    result = await world.click(
        alert_ref(RUN_ALERT, "exec-2"), RESTART, execution="exec-2", workflow="wf-2"
    )

    assert result.refusal is Refusal.UNVERIFIED
    assert world.restarts == []


async def test_an_administrator_fix_done_resolves_the_administrator_post(monkeypatch):
    world = _World(monkeypatch)
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage())

    refused = await world.admin_click(alert, FIX_DONE, user=APPROVER)
    validated = await world.admin_click(alert, VALIDATE)
    world.path.hop = ""
    fixed = await world.admin_click(alert, FIX_DONE)

    assert refused.refusal is Refusal.NOT_ALLOWED
    assert validated.accepted
    assert world.posts.replies == [("200.1", await world._validate_platform())]
    assert fixed.applied and fixed.alert
    [(ts, blocks)] = world.posts.updates
    assert ts == "200.1"
    assert any(block.get("block_id") == RESOLVED_BLOCK for block in blocks)
    assert world.episodes.resolvers == [f"slack:{ORG}/U0ADMIN"]
    assert [ts for ts, _ in world.edits] == ["100.1"]
    assert world.intake_triggers == 1
    assert {event.actor_type for event in world.audits} == {SLACK_ADMIN_ACTOR_TYPE}


ADMIN_OWNER_TOKEN = "owner-of-200.1"


class _AdminDelivery:
    def __init__(self, monkeypatch) -> None:
        from services.orchestration import notifications, platform_escalation
        from services.orchestration.notifications import SlackPostRef

        self.emails: list[str] = []
        self.slack: list[tuple[str, str]] = []
        self.thread_tokens: list[str] = []
        delivery = self

        class _SMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send_message(self, email):
                delivery.emails.append(email["Subject"])

        def post_admin(message, settings):
            self.slack.append((message.thread_ts, message.subject))
            if message.thread_ts:
                self.thread_tokens.append(message.thread_owner_token)
            else:
                message.posts.append(
                    SlackPostRef("", "200.1", admin=True, owner_token=ADMIN_OWNER_TOKEN)
                )
            return True

        monkeypatch.setattr(notifications.smtplib, "SMTP", _SMTP)
        monkeypatch.setattr(notifications, "_post_admin_slack", post_admin)
        monkeypatch.setattr(
            platform_escalation,
            "process_admin_destination",
            lambda: platform_escalation.AdminDestination(
                slack_channel="C0ADMIN", emails=("ops@example.org",)
            ),
        )

    def recoveries(self) -> tuple[list[str], list[str]]:
        return (
            [ts for ts, subject in self.slack if "recovered" in subject],
            [subject for subject in self.emails if "recovered" in subject],
        )


@pytest.mark.parametrize("source", ["tenant_fix_done", "admin_fix_done", "intake"])
async def test_each_resolution_resolves_the_administrator_post_and_emails_once(monkeypatch, source):
    admins = _AdminDelivery(monkeypatch)
    world = _World(monkeypatch, smtp_host="smtp.example.org", alert_email_from="adp@example.org")
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage())
    assert world.episodes.open_episodes["acme"].admin_slack_ts == "200.1"

    world.path.hop = ""
    if source == "tenant_fix_done":
        assert (await world.click(alert, FIX_DONE)).applied
    elif source == "admin_fix_done":
        assert (await world.admin_click(alert, FIX_DONE)).applied
    else:
        world.path.next_pass()
        await world.gate.admit(issue_key="T-2")
    world.path.next_pass()
    await world.gate.admit(issue_key="T-3")

    edits = {ts: blocks for ts, blocks in world.edits}
    assert sorted(edits) == ["100.1", "200.1"]
    assert all(blocks[-1]["block_id"] == RESOLVED_BLOCK for blocks in edits.values())
    assert world.posts.updates == []
    slack_replies, emails = admins.recoveries()
    assert slack_replies == ["200.1"]
    assert len(emails) == 1


async def test_the_admin_post_owner_token_is_stored_and_presented_by_its_edit_and_reply(
    monkeypatch,
):
    admins = _AdminDelivery(monkeypatch)
    world = _World(monkeypatch, smtp_host="smtp.example.org", alert_email_from="adp@example.org")
    await world.open_outage()

    assert world.episodes.open_episodes["acme"].admin_slack_token == ADMIN_OWNER_TOKEN

    world.path.hop = ""
    world.path.next_pass()
    await world.gate.admit(issue_key="T-2")

    [admin_edit] = [post for post in world.edited_posts if post.admin]
    assert (admin_edit.ts, admin_edit.owner_token) == ("200.1", ADMIN_OWNER_TOKEN)
    assert admins.thread_tokens == [ADMIN_OWNER_TOKEN]


async def test_restart_run_from_a_plan_rejected_post_restarts_once_under_double_clicks(monkeypatch):
    world = _World(monkeypatch)
    structured = remedies.plan_rejected_remedy("T-1")
    context = AlertContext(
        tenant="acme",
        alert=alert_ref(RUN_ALERT, "exec-1"),
        issued_at=int(time.time()),
        ttl_hours=1,
        action_key=KEY,
        execution_id="exec-1",
        workflow_id="wf-1",
    )
    alert = SlackAlert("T-1", "repo", "T-1", "plan", "plan_rejected", "", "", structured)
    _, blocks = alert_payload(alert, content="full", park=None, alert_context=context)

    def restart():
        return world.click(
            alert_ref(RUN_ALERT, "exec-1"),
            RESTART,
            execution="exec-1",
            workflow="wf-1",
            blocks=blocks,
        )

    concurrent = await asyncio.gather(restart(), restart())
    later = await restart()

    assert world.restarts == ["exec-1"]
    assert sorted(str(result.refusal) for result in concurrent) == [
        "None",
        str(Refusal.ALREADY_RESTARTED),
    ]
    assert later.refusal is Refusal.ALREADY_RESTARTED
    assert [event.decision for event in world.audits] == ["restarted"]
    assert world.posts.updates
    for _, edited in world.posts.updates:
        assert not any(block["type"] == "actions" for block in edited)
        assert "plan_rejected" in _texts(edited)
        line = _context_text(edited, PostState.RESTARTED.block_id)
        assert line.startswith(f"Restarted by <@{APPROVER}> at ")
        assert "&lt;@" not in line and "slack:" not in line


async def test_a_click_on_a_resolved_episode_whose_post_shows_buttons_edits_it_to_resolved(
    monkeypatch,
):
    world = _World(monkeypatch)
    stored = _outage_blocks()
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage(json.dumps(stored)))
    world.path.hop = ""
    await world.click(alert, FIX_DONE)
    world.posts.updates.clear()

    stale = await world.click(alert, ACKNOWLEDGE, blocks=stored)

    assert stale.refusal is Refusal.RESOLVED
    [(ts, edited)] = world.posts.updates
    assert ts == "100.1"
    assert not any(block["type"] == "actions" for block in edited)
    assert "The Bedrock connection fails." in _texts(edited)
    assert _context_text(edited, RESOLVED_BLOCK) == "Resolved."


async def test_an_acknowledgement_then_a_resolution_keeps_the_post_and_both_lines(monkeypatch):
    world = _World(monkeypatch)
    stored = _outage_blocks()
    alert = alert_ref(MODEL_PATH_ALERT, await world.open_outage(json.dumps(stored)))

    await world.click(alert, ACKNOWLEDGE, blocks=stored)
    world.path.hop = ""
    await world.click(alert, FIX_DONE)

    ts, resolved = world.edits[-1]
    assert ts == "100.1"
    assert not any(block["type"] == "actions" for block in resolved)
    for label, value in OUTAGE_LINES:
        assert label in _texts(resolved) and value in _texts(resolved)
    assert f"<@{APPROVER}>" in _context_text(resolved, ACKNOWLEDGED_BLOCK)
    line = _context_text(resolved, RESOLVED_BLOCK)
    assert line.startswith(f"Resolved by <@{APPROVER}> at ")
    assert line.endswith(". Fix done pressed.")
    assert "&lt;@" not in line and "slack:" not in line
