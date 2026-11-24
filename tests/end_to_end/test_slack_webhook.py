"""A Slack click reaches the inbound webhook and decides its park.

A click of a tenant's own app arrives at /webhook/slack/<tenant>, and a click of the shared app
arrives at /webhook/slack/_shared and is decided by the tenant whose escalation.yaml names its
channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration import tenant_config
from services.integration.repositories import GITHUB
from services.integration.slack.dispatcher import (
    RecordedDecision,
    Refusal,
    TemporalWorkflowDecisions,
)
from services.integration.slack.tokens import ActionToken, sign_action_token
from services.orchestration.temporal_workflows.draft_pull_request import ParkDecisionOutcome
from services.orchestration.webhook.server import (
    TenantSlackSettings,
    build_handler,
    build_routes_for,
    shared_slack_routes,
    slack_routes,
)
from services.orchestration.webhook.slack_socket import SharedSlackSocket
from tests.support import SHARED_APP_ID, SHARED_APP_YAML, SHARED_ORGANIZATION_ID, hermetic_settings
from tests.unit.test_slack_tenant_settings import BUCKET, EARLY, LATE, _Bucket
from tests.unit.test_tenant_claims_store import marker_key
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
WORKFLOW_TENANTS = {WORKFLOW_ID: "adp", "wf-beta-1": "beta", "wf-gamma-1": "gamma"}
SHARED_ROUTE = "/webhook/slack/_shared"
SHARED_SIGNING = "shared-slack-signing"
SHARED_ORG = SHARED_ORGANIZATION_ID
SHARED_APP = SHARED_APP_YAML
SHARED_ESCALATION = """config_version: 1
channels: [jira, slack]
slack:
  app: shared
  transport: webhook
  channel: {channel}
  profile: private
  allowlist:
    gate: [{{user_id: U0{name}}}]
"""
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
        return WORKFLOW_TENANTS.get(workflow_id)

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
    try:
        with _serving(routes, settings) as port:
            yield _Webhook(port, client, records, hooks, starter, signaller)
    finally:
        tenant_config.reset()


@contextmanager
def _serving(routes: Mapping[str, Any], settings: Any) -> Iterator[int]:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), build_handler(routes, InMemorySideEffectLedger(), settings, loop)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)


def _token(tenant: str = "adp", key: str = ADP_ACTION_KEY) -> str:
    token = ActionToken(
        tenant=tenant,
        execution_id="exec-1",
        workflow_id=f"wf-{tenant}-1",
        park_key=PARK_KEY,
        kind="gate",
        actions=("approve", "reject"),
        expires_at=int(time.time()) + 3600,
    )
    return sign_action_token(token, key.encode())


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


@dataclass
class _SharedWebhook(_Webhook):
    slack: TenantSlackSettings
    routing: Any
    bucket: _Bucket
    shared_route: Any

    def publish(self, tenant: str, escalation: str, channel: str) -> None:
        """Claim the channel and upload the tenant's escalation.yaml, as a publish does."""
        self.bucket.markers.setdefault(marker_key("slack-channel", channel, tenant), LATE)
        self.routing.source(tenant).files["escalation"] = escalation
        self.slack.refresh()


def _shared_escalation(tenant: str, channel: str) -> str:
    return SHARED_ESCALATION.format(channel=channel, name=tenant.upper())


def _own_escalation(tenant: str, channel: str | None = None) -> str:
    text = ESCALATION.format(transport="webhook", name=tenant.upper())
    return text if channel is None else text.replace(f"C0{tenant.upper()}", channel)


DEFAULT_MARKERS = {
    marker_key("slack-channel", "C1", "adp"): EARLY,
    marker_key("slack-channel", "C2", "beta"): EARLY,
}


@pytest.fixture
def shared_webhook(tmp_path, monkeypatch) -> Iterator[Callable[..., _SharedWebhook]]:
    env = tmp_path / "env"
    env.mkdir()
    (env / "integrations.yaml").write_text(INTEGRATIONS)
    (env / "slack-shared-app.yaml").write_text(SHARED_APP)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    for name, value in {
        **ENVIRON,
        "SLACK_SIGNING_SECRET_GAMMA": "gamma-slack-signing",
        "ESCALATION_ACTION_KEY_GAMMA": "gamma-action-key",
    }.items():
        monkeypatch.setenv(name, value)
    hooks = _Hooks()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, request: hooks.handle_request(request)
    )
    tenant_config.reset()

    def start(
        escalations: Mapping[str, str] | None = None,
        markers: Mapping[str, datetime] | None = None,
    ) -> _SharedWebhook:
        settings = hermetic_settings(
            tenants="adp,beta,gamma",
            tenant="adp",
            tenant_config_bucket=BUCKET,
            slack_shared_signing_secret=SHARED_SIGNING,
        )
        files = {
            "adp": _shared_escalation("adp", "C1"),
            "beta": _shared_escalation("beta", "C2"),
            "gamma": _own_escalation("gamma"),
            **(escalations or {}),
        }
        routing = _routing(
            {"adp": {"adp-web": GITHUB}, "beta": {}, "gamma": {}},
            {"adp": _jira_yaml("ADP")},
            settings=settings,
            files={tenant: {"escalation": text} for tenant, text in files.items()},
        )
        bucket = _Bucket()
        bucket.markers.update(DEFAULT_MARKERS if markers is None else markers)
        slack = TenantSlackSettings(settings, routing, claims_client=bucket)
        slack.refresh()
        client, records = _TemporalClient(), _Records()
        starter, signaller = _Starter(), _Signaller()
        routes = {
            **build_routes_for(
                settings,
                routing=routing,
                signaller=signaller,
                starter=starter,
                resolver=_Resolver(),
            ),
            **slack_routes(
                settings,
                routing,
                slack=slack.slack,
                decisions=TemporalWorkflowDecisions(client),
                records=records,
            ),
            **shared_slack_routes(
                settings,
                routing,
                slack=slack.slack,
                channel_tenant=slack.channel_tenant,
                shared_app=slack.shared_app,
                decisions=TemporalWorkflowDecisions(client),
                records=records,
                retained_holder=slack.retained_holder,
            ),
        }
        port = servers.enter_context(_serving(routes, settings))
        return _SharedWebhook(
            port,
            client,
            records,
            hooks,
            starter,
            signaller,
            slack,
            routing,
            bucket,
            routes[SHARED_ROUTE],
        )

    with ExitStack() as servers:
        yield start
    tenant_config.reset()


def _shared_click(
    channel: str | None = "C1", tenant: str = "adp", key: str = ADP_ACTION_KEY, **overrides: Any
) -> bytes:
    payload: dict[str, Any] = {
        "type": "block_actions",
        "api_app_id": SHARED_APP_ID,
        "team": {"id": "T0WORKSPACE"},
        "enterprise": {"id": SHARED_ORG},
        "user": {"id": f"U0{tenant.upper()}", "team_id": "T0WORKSPACE"},
        "channel": {"id": channel},
        "actions": [{"action_id": "approve", "value": _token(tenant, key)}],
        "response_url": RESPONSE_URL,
        **overrides,
    }
    if channel is None:
        del payload["channel"]
    return urlencode({"payload": json.dumps(payload)}).encode()


def _post_shared(webhook: _Webhook, body: bytes) -> int:
    return webhook.post(SHARED_ROUTE, body, _signed(body, secret=SHARED_SIGNING))[0]


def _actors(webhook: _Webhook) -> list[str]:
    return [args[2] for args in webhook.client.applied]


def test_each_shared_app_click_resolves_the_park_of_the_tenant_that_names_its_channel(
    shared_webhook,
):
    webhook = shared_webhook()

    adp_status = _post_shared(webhook, _shared_click("C1", "adp"))
    beta_status = _post_shared(webhook, _shared_click("C2", "beta", "beta-action-key"))

    assert (adp_status, beta_status) == (200, 200)
    assert webhook.client.applied == [
        [PARK_KEY, True, "slack:E0SHARED001/U0ADP", "slack"],
        [PARK_KEY, True, "slack:E0SHARED001/U0BETA", "slack"],
    ]
    webhook.hooks.wait(2)
    assert all(message["replace_original"] for _, message in webhook.hooks.posts)


def _socket_payload(body: bytes) -> dict[str, Any]:
    return json.loads(parse_qs(body.decode())["payload"][0])


def test_a_shared_app_click_over_socket_mode_resolves_the_park_as_a_request_url_click_does(
    shared_webhook,
):
    webhook = shared_webhook()
    socket = SharedSlackSocket(
        webhook.shared_route, "xapp-shared", webhook.slack.shared_app, asyncio.new_event_loop()
    )

    asyncio.run(socket.handle(_socket_payload(_shared_click("C2", "beta", "beta-action-key"))))
    asyncio.run(socket.handle(_socket_payload(_shared_click("C1", "beta", "beta-action-key"))))

    assert webhook.client.applied == [[PARK_KEY, True, "slack:E0SHARED001/U0BETA", "slack"]]
    webhook.hooks.wait(2)
    assert webhook.hooks.posts[0][1]["replace_original"]


def test_a_shared_app_click_on_a_channel_no_tenant_names_is_refused(shared_webhook):
    webhook = shared_webhook()

    status = _post_shared(webhook, _shared_click("C3"))

    assert status == 200
    assert webhook.client.applied == []
    [refusal] = webhook.records.refusals
    assert (refusal.reason, refusal.route, refusal.channel) == (
        Refusal.UNROUTED,
        SHARED_ROUTE,
        "C3",
    )
    webhook.hooks.wait(1)
    url, message = webhook.hooks.posts[0]
    assert url == RESPONSE_URL
    assert message["response_type"] == "ephemeral"


@pytest.mark.parametrize(
    ("markers", "applied"),
    [
        ({}, False),
        (
            {
                marker_key("slack-channel", "C1", "adp"): EARLY,
                marker_key("slack-channel", "C1", "beta"): LATE,
            },
            True,
        ),
    ],
    ids=["no-deciding-marker", "adp-marker-earliest"],
)
def test_a_channel_two_shared_tenants_name_routes_by_the_earliest_claim(
    shared_webhook, markers, applied
):
    webhook = shared_webhook({"beta": _shared_escalation("beta", "C1")}, markers)

    status = _post_shared(webhook, _shared_click("C1"))

    assert status == 200
    assert _actors(webhook) == (["slack:E0SHARED001/U0ADP"] if applied else [])
    reasons = [(refusal.reason, refusal.channel) for refusal in webhook.records.refusals]
    assert reasons == ([] if applied else [(Refusal.UNROUTED, "C1")])


@pytest.mark.parametrize(
    "request_for",
    [
        lambda: (_shared_click(), _signed(_shared_click(), secret=ADP_SIGNING)),
        lambda: (
            _shared_click(),
            _signed(_shared_click(), secret=SHARED_SIGNING, timestamp=str(int(time.time()) - 301)),
        ),
        lambda: (
            _shared_click(api_app_id="A0ADP"),
            _signed(_shared_click(api_app_id="A0ADP"), secret=SHARED_SIGNING),
        ),
    ],
    ids=["tenant-signing-secret", "stale-timestamp", "another-app"],
)
def test_an_unverified_shared_app_request_is_refused(shared_webhook, request_for):
    webhook = shared_webhook()
    body, headers = request_for()

    status, _ = webhook.post(SHARED_ROUTE, body, headers)

    assert status == 401
    assert webhook.client.applied == []
    assert webhook.records.refusals == []


def test_a_shared_app_click_that_names_its_channel_only_in_the_container_routes(
    shared_webhook,
):
    webhook = shared_webhook()
    body = _shared_click(channel=None, container={"type": "message", "channel_id": "C1"})

    assert _post_shared(webhook, body) == 200
    assert _actors(webhook) == ["slack:E0SHARED001/U0ADP"]


def test_a_shared_app_click_carrying_another_tenants_token_is_unverified(shared_webhook):
    webhook = shared_webhook()
    body = _shared_click("C1", "beta", "beta-action-key", user={"id": "U0ADP"})

    assert _post_shared(webhook, body) == 200
    assert webhook.client.applied == []
    assert [(r.tenant, r.reason) for r in webhook.records.refusals] == [("adp", Refusal.UNVERIFIED)]


def test_an_org_install_click_without_a_team_routes_on_its_enterprise_and_channel(
    shared_webhook,
):
    webhook = shared_webhook()
    body = _shared_click(team=None, user={"id": "U0ADP", "team_id": "T0OTHER"})

    assert _post_shared(webhook, body) == 200
    assert _actors(webhook) == ["slack:E0SHARED001/U0ADP"]


def _gamma_click(app_id: str = "A0GAMMA") -> bytes:
    payload = {
        "type": "block_actions",
        "api_app_id": app_id,
        "user": {"id": "U0GAMMA", "team_id": "T0GAMMA"},
        "channel": {"id": "C0GAMMA"},
        "actions": [{"action_id": "approve", "value": _token("gamma", "gamma-action-key")}],
        "response_url": RESPONSE_URL,
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def test_a_per_tenant_app_route_serves_beside_the_shared_route(shared_webhook):
    webhook = shared_webhook()
    gamma = _gamma_click()

    gamma_status, _ = webhook.post(
        "/webhook/slack/gamma", gamma, _signed(gamma, secret="gamma-slack-signing")
    )
    shared_status = _post_shared(webhook, _shared_click("C1"))

    assert (gamma_status, shared_status) == (200, 200)
    assert _actors(webhook) == ["slack:T0GAMMA/U0GAMMA", "slack:E0SHARED001/U0ADP"]


def test_a_tenant_that_moves_to_the_shared_app_after_start_has_its_clicks_resolved(
    shared_webhook,
):
    webhook = shared_webhook()

    webhook.publish("gamma", _shared_escalation("gamma", "C4"), "C4")
    status = _post_shared(webhook, _shared_click("C4", "gamma", "gamma-action-key"))

    assert status == 200
    assert _actors(webhook) == ["slack:E0SHARED001/U0GAMMA"]


def test_a_malformed_shared_app_at_start_refuses_only_the_shared_route_until_it_is_fixed(
    shared_webhook,
):
    shared_app = Path(os.environ["PLATFORM_ENV_CONFIG_DIR"]) / "slack-shared-app.yaml"
    shared_app.write_text("config_version: 1\napp_id: [\n")
    webhook = shared_webhook()
    pr, jira, gamma = _pr_body("adp-web"), _jira_body("ADP-7"), _gamma_click()

    pr_status, _ = webhook.post(
        "/webhook/pr",
        pr,
        {"X-Hub-Signature-256": _sign(ADP_GITHUB_KEY, pr), "X-GitHub-Delivery": "d-1"},
    )
    jira_status, _ = webhook.post(
        "/webhook/jira/adp", jira, {"X-Hub-Signature": _sign(ADP_JIRA_KEY, jira)}
    )
    gamma_status, _ = webhook.post(
        "/webhook/slack/gamma", gamma, _signed(gamma, secret="gamma-slack-signing")
    )
    shared_status = _post_shared(webhook, _shared_click("C1"))

    assert (pr_status, jira_status, gamma_status, shared_status) == (200, 200, 200, 503)

    shared_app.write_text(SHARED_APP)
    webhook.slack.refresh()

    assert _post_shared(webhook, _shared_click("C1")) == 200
    assert _actors(webhook) == ["slack:T0GAMMA/U0GAMMA", "slack:E0SHARED001/U0ADP"]


def test_a_changed_shared_app_id_verifies_clicks_against_the_new_id_after_a_refresh(
    shared_webhook,
):
    webhook = shared_webhook()
    shared_app = Path(os.environ["PLATFORM_ENV_CONFIG_DIR"]) / "slack-shared-app.yaml"
    shared_app.write_text(SHARED_APP.replace(SHARED_APP_ID, "A0SHARED002"))
    webhook.slack.refresh()

    assert _post_shared(webhook, _shared_click("C1")) == 401
    assert _post_shared(webhook, _shared_click("C1", api_app_id="A0SHARED002")) == 200


def _moved_from_c1(shared_webhook, *, retained: bool) -> _SharedWebhook:
    """Start the webhook after adp moved from C1 to C3, keeping or releasing its C1 claim."""
    markers = {
        **DEFAULT_MARKERS,
        marker_key("slack-channel", "C3", "adp"): LATE,
    }
    if not retained:
        del markers[marker_key("slack-channel", "C1", "adp")]
    return shared_webhook({"adp": _shared_escalation("adp", "C3")}, markers)


def test_a_click_on_a_button_in_a_channel_the_tenant_left_resolves_its_park(shared_webhook):
    webhook = _moved_from_c1(shared_webhook, retained=True)

    status = _post_shared(webhook, _shared_click("C1"))

    assert status == 200
    assert _actors(webhook) == ["slack:E0SHARED001/U0ADP"]
    assert webhook.slack.channel_tenant("C1") is None


def test_a_click_from_a_retained_channel_signed_with_another_tenants_key_is_unverified(
    shared_webhook,
):
    webhook = _moved_from_c1(shared_webhook, retained=True)

    status = _post_shared(webhook, _shared_click("C1", "beta", "beta-action-key"))

    assert status == 200
    assert webhook.client.applied == []
    assert [refusal.reason for refusal in webhook.records.refusals] == [Refusal.UNVERIFIED]


def test_a_click_from_a_released_channel_is_refused_as_unrouted(shared_webhook):
    webhook = _moved_from_c1(shared_webhook, retained=False)

    status = _post_shared(webhook, _shared_click("C1"))

    assert status == 200
    assert webhook.client.applied == []
    [refusal] = webhook.records.refusals
    assert (refusal.reason, refusal.route, refusal.channel) == (
        Refusal.UNROUTED,
        SHARED_ROUTE,
        "C1",
    )


def test_the_per_tenant_route_of_a_shared_app_tenant_is_not_found(shared_webhook):
    webhook = shared_webhook(
        {"gamma": _shared_escalation("gamma", "C4")},
        {**DEFAULT_MARKERS, marker_key("slack-channel", "C4", "gamma"): EARLY},
    )
    body = _gamma_click(SHARED_APP_ID)

    status, _ = webhook.post(
        "/webhook/slack/gamma", body, _signed(body, secret="gamma-slack-signing")
    )

    assert status == 404
    assert webhook.client.applied == []


@pytest.mark.parametrize(
    ("gamma_marker", "applied"),
    [(LATE, True), (EARLY, False)],
    ids=["own-app-marker-later", "own-app-marker-earlier"],
)
def test_an_own_app_tenant_naming_a_shared_channel_routes_by_the_earliest_claim(
    shared_webhook, gamma_marker, applied
):
    webhook = shared_webhook(
        {"gamma": _own_escalation("gamma", "C1")},
        {
            marker_key("slack-channel", "C1", "adp"): EARLY if gamma_marker == LATE else LATE,
            marker_key("slack-own-channel", "C1", "gamma"): gamma_marker,
        },
    )

    status = _post_shared(webhook, _shared_click("C1"))

    assert status == 200
    assert _actors(webhook) == (["slack:E0SHARED001/U0ADP"] if applied else [])
    assert (webhook.slack.channel_tenant("C1") == "adp") is applied
