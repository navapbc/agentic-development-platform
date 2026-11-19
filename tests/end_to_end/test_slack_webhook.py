"""A Slack click reaches the inbound webhook at /webhook/slack/<tenant> and decides its park."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration import tenant_config
from services.integration.repositories import GITHUB
from services.integration.slack.dispatcher import RecordedDecision, TemporalWorkflowDecisions
from services.integration.slack.tokens import ActionToken, sign_action_token
from services.orchestration.temporal_workflows.draft_pull_request import ParkDecisionOutcome
from services.orchestration.webhook.server import (
    TenantSlackSettings,
    build_handler,
    build_routes_for,
    slack_routes,
)
from tests.support import hermetic_settings
from tests.unit.test_webhook_tenants import (
    _jira_body,
    _jira_yaml,
    _pr_body,
    _Resolver,
    _routing,
    _sign,
    _Signaller,
    _Starter,
)

ADP_SIGNING = "adp-slack-signing"
ADP_ACTION_KEY = "adp-action-key"
ADP_JIRA_KEY = "adp-jira-key"
ADP_GITHUB_KEY = "adp-github-key"
ENVIRON = {
    "SLACK_SIGNING_SECRET_ADP": ADP_SIGNING,
    "SLACK_SIGNING_SECRET_BETA": "beta-slack-signing",
    "ESCALATION_ACTION_KEY_ADP": ADP_ACTION_KEY,
    "ESCALATION_ACTION_KEY_BETA": "beta-action-key",
    "JIRA_WEBHOOK_SECRET_ADP": ADP_JIRA_KEY,
    "GITHUB_WEBHOOK_SECRET_ADP": ADP_GITHUB_KEY,
}
INTEGRATIONS = """config_version: 1
modes:
  pr_closed: webhook
  jira_intake: webhook
"""
ESCALATION = """config_version: 1
channels: [jira, slack]
slack:
  transport: {transport}
  channel: C0{name}
  organization_id: T0{name}
  app_id: A0{name}
  profile: private
  allowlist:
    gate: [{{user_id: U0{name}}}]
"""
RESPONSE_URL = "https://hooks.slack.com/actions/T0ADP/1/abc"
WORKFLOW_ID = "wf-adp-1"
PARK_KEY = "exec-1:review:security:1"


@dataclass
class _Handle:
    client: _TemporalClient
    workflow_id: str

    async def execute_update(
        self, update: str, *, args: list[Any], id: str, result_type: type
    ) -> Any:
        pending = self.client.updates.get(id)
        if pending is None:
            pending = self.client.updates[id] = asyncio.ensure_future(self.client.run(args))
        return await asyncio.shield(pending)

    async def query(self, name: str) -> dict[str, Any]:
        return {"park_key": PARK_KEY}


@dataclass
class _TemporalClient:
    delay: float = 0.0
    applied: list[list[Any]] = field(default_factory=list)
    updates: dict[str, asyncio.Future] = field(default_factory=dict)

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        return _Handle(self, workflow_id)

    async def run(self, args: list[Any]) -> ParkDecisionOutcome:
        await asyncio.sleep(self.delay)
        self.applied.append(args)
        return ParkDecisionOutcome(
            decision="approved", actor=args[2], actor_type=args[3], applied=True
        )


@dataclass
class _Records:
    refusals: list[Any] = field(default_factory=list)

    async def workflow_tenant(self, execution_id: str, workflow_id: str) -> str | None:
        return "adp" if workflow_id == WORKFLOW_ID else None

    async def decision(self, event_id: str) -> RecordedDecision | None:
        return None

    async def record_refusal(self, refusal: Any) -> None:
        self.refusals.append(refusal)


@dataclass
class _Hooks:
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    answer: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(200)
    arrived: threading.Event = field(default_factory=threading.Event)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((str(request.url), json.loads(request.content or b"{}")))
        self.arrived.set()
        return self.answer(request)

    def wait(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.posts) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.posts) >= count


@dataclass
class _Webhook:
    port: int
    client: _TemporalClient
    records: _Records
    hooks: _Hooks
    starter: _Starter
    signaller: _Signaller

    def post(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


@pytest.fixture
def webhook(tmp_path, monkeypatch) -> Iterator[_Webhook]:
    env = tmp_path / "env"
    env.mkdir()
    (env / "integrations.yaml").write_text(INTEGRATIONS)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    hooks = _Hooks()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, request: hooks.handle_request(request)
    )
    tenant_config.reset()

    settings = hermetic_settings(tenants="adp,beta", tenant="adp")
    routing = _routing(
        {"adp": {"adp-web": GITHUB}, "beta": {}},
        {"adp": _jira_yaml("ADP")},
        settings=settings,
        files={
            tenant: {"escalation": ESCALATION.format(transport=transport, name=tenant.upper())}
            for tenant, transport in {"adp": "webhook", "beta": "socket"}.items()
        },
    )
    slack = TenantSlackSettings(settings, routing)
    slack.refresh()
    client, records, starter, signaller = _TemporalClient(), _Records(), _Starter(), _Signaller()
    routes = {
        **build_routes_for(
            settings, routing=routing, signaller=signaller, starter=starter, resolver=_Resolver()
        ),
        **slack_routes(
            settings,
            routing,
            slack=slack.slack,
            decisions=TemporalWorkflowDecisions(client),
            records=records,
        ),
    }
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), build_handler(routes, InMemorySideEffectLedger(), settings, loop)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield _Webhook(server.server_address[1], client, records, hooks, starter, signaller)
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        tenant_config.reset()


def _token() -> str:
    token = ActionToken(
        tenant="adp",
        execution_id="exec-1",
        workflow_id=WORKFLOW_ID,
        park_key=PARK_KEY,
        kind="gate",
        actions=("approve", "reject"),
        expires_at=int(time.time()) + 3600,
    )
    return sign_action_token(token, ADP_ACTION_KEY.encode())


def _click(response_url: str = RESPONSE_URL, **overrides: Any) -> bytes:
    payload = {
        "type": "block_actions",
        "api_app_id": "A0ADP",
        "user": {"id": "U0ADP", "team_id": "T0ADP"},
        "channel": {"id": "C0ADP"},
        "actions": [{"action_id": "approve", "value": _token()}],
        "response_url": response_url,
        **overrides,
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def _signed(body: bytes, *, secret: str = ADP_SIGNING, timestamp: str | None = None) -> dict:
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{stamp}:".encode() + body, hashlib.sha256)
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": f"v0={digest.hexdigest()}",
    }


def test_a_signed_click_returns_promptly_and_applies_the_decision_once(webhook: _Webhook):
    body = _click()

    started = time.monotonic()
    status, content = webhook.post("/webhook/slack/adp", body, _signed(body))

    assert (status, content) == (200, b"")
    assert time.monotonic() - started < 3.0
    assert webhook.client.applied == [[PARK_KEY, True, "slack:T0ADP/U0ADP", "slack"]]
    webhook.hooks.wait(1)
    url, message = webhook.hooks.posts[0]
    assert url == RESPONSE_URL
    assert message["replace_original"] is True
    assert "Approved by slack:T0ADP/U0ADP." in json.dumps(message["blocks"])
    assert all(block["type"] != "actions" for block in message["blocks"])


@pytest.mark.parametrize(
    "headers",
    [
        _signed(b"not a form", secret="another-secret"),
        _signed(b"not a form", timestamp=str(int(time.time()) - 301)),
        _signed(b"not a form", timestamp="soon"),
        {"X-Slack-Signature": "v0=00"},
    ],
    ids=["invalid-signature", "stale-timestamp", "non-numeric-timestamp", "missing-timestamp"],
)
def test_an_unverified_request_is_refused_before_its_body_is_decoded(webhook, headers):
    status, _ = webhook.post("/webhook/slack/adp", b"not a form", headers)

    assert status == 401
    assert webhook.client.applied == []


def test_a_signed_body_that_is_not_a_slack_form_is_malformed(webhook: _Webhook):
    status, _ = webhook.post("/webhook/slack/adp", b"not a form", _signed(b"not a form"))

    assert status == 400


def test_a_click_from_another_slack_app_is_refused(webhook: _Webhook):
    body = _click(api_app_id="A0OTHER")

    status, _ = webhook.post("/webhook/slack/adp", body, _signed(body))

    assert status == 401
    assert webhook.client.applied == []


def test_a_payload_other_than_a_block_action_is_ignored(webhook: _Webhook):
    body = _click(type="view_submission")

    status, _ = webhook.post("/webhook/slack/adp", body, _signed(body))

    assert status == 200
    assert webhook.client.applied == []


def test_a_tenant_whose_slack_transport_is_not_webhook_has_no_slack_route(webhook: _Webhook):
    body = _click(api_app_id="A0BETA")

    status, _ = webhook.post(
        "/webhook/slack/beta", body, _signed(body, secret="beta-slack-signing")
    )

    assert status == 404


def test_slack_needs_no_integrations_entry_and_a_pr_delivery_still_passes_handle_inbound(
    webhook: _Webhook,
):
    click = _click()
    pr = _pr_body("adp-web")

    slack_status, _ = webhook.post("/webhook/slack/adp", click, _signed(click))
    pr_status, pr_content = webhook.post(
        "/webhook/pr",
        pr,
        {"X-Hub-Signature-256": _sign(ADP_GITHUB_KEY, pr), "X-GitHub-Delivery": "d-1"},
    )

    assert slack_status == 200
    assert len(webhook.client.applied) == 1
    assert pr_status == 200
    assert json.loads(pr_content) == {
        "status": "signalled",
        "workflow_id": "wf-adp-T-7",
        "merged": True,
    }
    assert webhook.signaller.signals == ["wf-adp-T-7"]


def test_a_duplicate_click_in_flight_receives_the_first_outcome(webhook: _Webhook):
    webhook.client.delay = 0.5
    body = _click()
    statuses: list[int] = []

    def click() -> None:
        statuses.append(webhook.post("/webhook/slack/adp", body, _signed(body))[0])

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [200, 200]
    assert len(webhook.client.applied) == 1
    webhook.hooks.wait(2)
    first, second = (message for _, message in webhook.hooks.posts)
    assert first == second
    assert first["replace_original"] is True


def test_an_update_that_exceeds_its_bound_receives_the_not_yet_confirmed_reply(
    webhook: _Webhook,
):
    webhook.client.delay = 60
    body = _click()

    started = time.monotonic()
    status, _ = webhook.post("/webhook/slack/adp", body, _signed(body))

    assert status == 200
    assert time.monotonic() - started < 3.0
    webhook.hooks.wait(1)
    _, message = webhook.hooks.posts[0]
    assert message["response_type"] == "ephemeral"
    assert message["replace_original"] is False
    assert message["text"].startswith("The decision is not yet confirmed.")


@pytest.mark.parametrize(
    "response_url",
    ["https://hooks.example.com/actions/T0ADP/1/abc", "http://hooks.slack.com/actions/T0ADP/1/abc"],
    ids=["another-host", "http"],
)
def test_a_response_url_outside_the_slack_hooks_origin_is_not_called(webhook, response_url):
    body = _click(response_url)

    status, _ = webhook.post("/webhook/slack/adp", body, _signed(body))
    time.sleep(0.3)

    assert status == 200
    assert len(webhook.client.applied) == 1
    assert webhook.hooks.posts == []


def test_a_response_url_that_redirects_is_not_followed(webhook: _Webhook, caplog):
    webhook.hooks.answer = lambda request: httpx.Response(
        302, headers={"Location": "https://hooks.slack.com/actions/elsewhere"}
    )
    body = _click()

    with caplog.at_level("WARNING"):
        status, _ = webhook.post("/webhook/slack/adp", body, _signed(body))
        webhook.hooks.wait(1)
        time.sleep(0.3)

    assert status == 200
    assert [url for url, _ in webhook.hooks.posts] == [RESPONSE_URL]
    assert any("adp" in record.getMessage() for record in caplog.records)
    assert all(RESPONSE_URL not in record.getMessage() for record in caplog.records)


def test_a_response_url_that_hangs_does_not_delay_a_jira_delivery(webhook: _Webhook):
    release = threading.Event()

    def hang(request: httpx.Request) -> httpx.Response:
        release.wait(10)
        return httpx.Response(200)

    webhook.hooks.answer = hang
    click = _click()
    jira = _jira_body("ADP-7")
    try:
        assert webhook.post("/webhook/slack/adp", click, _signed(click))[0] == 200
        assert webhook.hooks.arrived.wait(5)

        started = time.monotonic()
        status, _ = webhook.post(
            "/webhook/jira/adp", jira, {"X-Hub-Signature": _sign(ADP_JIRA_KEY, jira)}
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert status == 200
    assert elapsed < 1.0
    assert [event.key for event in webhook.starter.events] == ["ADP-7"]
