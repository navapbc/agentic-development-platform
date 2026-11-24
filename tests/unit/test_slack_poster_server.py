"""The posting service posts a shared-app tenant's alert to that tenant's channel only."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing
from scripts.slack_poster_tls import issue_leaf, new_ca
from services.integration.slack.shared_app import SharedSlackApp
from services.orchestration.escalation import OWN_APP, SHARED_APP, SlackSettings
from services.server_tls import expires_at
from services.slack_poster import DEADLINE_HEADER, MESSAGES_PATH, TOKEN_AUDIENCE, server
from services.slack_poster.auth import ReviewUnavailable, WorkerIdentity
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web import SlackResponse
from tests.support import SHARED_APP_YAML, SHARED_ORGANIZATION_ID, hermetic_settings

ORGANIZATION = SHARED_ORGANIZATION_ID
SHARED_APP_FILE = SHARED_APP_YAML
GATES = "config_version: 1\nprofile: private\n"
TS = "1700000000.000100"
TEXT = "Gate approval needed. Next step: inv gate-approve act-SECRET-action-token"
BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "act-SECRET-action-token"}}]


def _status(namespace: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "audiences": [TOKEN_AUDIENCE],
        "user": {
            "username": f"system:serviceaccount:{namespace}:activity-worker",
            "extra": {
                "authentication.kubernetes.io/pod-name": ["activity-worker-abc"],
                "authentication.kubernetes.io/pod-uid": ["0b6c3f1e-pod"],
            },
        },
    }


REVIEWS: dict[str, dict[str, Any]] = {
    "adp-worker-SECRET": _status("tenant-adp"),
    "beta-worker-SECRET": _status("tenant-beta"),
}


def _review(token: str) -> Mapping[str, Any]:
    if token == "api-unreachable":
        raise ReviewUnavailable("connection refused")
    return REVIEWS.get(token, {"authenticated": False})


def _settings(channel: str, app: str = SHARED_APP) -> SlackSettings:
    return SlackSettings(
        transport="webhook",
        channel=channel,
        organization_id=ORGANIZATION,
        app_id="",
        profile="private",
        content="full",
        app=app,
    )


SHARED = SharedSlackApp(
    app_id="A0SHARED001", organization_id=ORGANIZATION, api_base_url="https://slack.com/api/"
)


@dataclass
class _Snapshot:
    settings: dict[str, SlackSettings] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)
    app: SharedSlackApp | None = SHARED

    def slack(self, tenant: str) -> SlackSettings | None:
        return self.settings.get(tenant)

    def channel_tenant(self, channel: str) -> str | None:
        return self.channels.get(channel)

    def shared_app(self) -> SharedSlackApp | None:
        return self.app


def _slack_error(code: str, status: int = 200, headers: dict[str, str] | None = None):
    return SlackApiError(
        code,
        SlackResponse(
            client=None,
            http_verb="POST",
            api_url="https://slack.com/api/chat.postMessage",
            req_args={},
            data={"ok": False, "error": code},
            headers=headers or {},
            status_code=status,
        ),
    )


class _Slack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.timeouts: list[int] = []
        self.error: Exception | None = None
        self.identity: Any = {"ok": True, "team_id": "T0WORKSPACE", "enterprise_id": ORGANIZATION}
        self.release: threading.Event | None = None
        self.entered = threading.Semaphore(0)

    def client(self, shared_app: SharedSlackApp, timeout: int) -> _Slack:
        self.timeouts.append(timeout)
        return self

    def auth_test(self) -> Any:
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def chat_postMessage(self, **kwargs: Any) -> Any:  # noqa: N802
        self.posts.append(kwargs)
        self.entered.release()
        if self.release is not None:
            self.release.wait(5)
        if self.error is not None:
            raise self.error
        return {"ok": True, "channel": kwargs["channel"], "ts": TS}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Poster:
    url: str
    slack: _Slack
    clock: _Clock
    service: server.PostingService

    def post(
        self,
        token: str | None = "adp-worker-SECRET",
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        content = json.dumps({"text": TEXT, "blocks": BLOCKS} if body is None else body)
        return httpx.post(f"{self.url}{MESSAGES_PATH}", content=content, headers=sent)

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.url}{path}")


def _serve(snapshot: Any, slack: _Slack | None = None) -> Iterator[_Poster]:
    slack = slack or _Slack()
    clock = _Clock()
    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp", "beta")),
        snapshot=snapshot,
        slack_client=slack.client,
        clock=clock,
    )
    service.check_slack()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.build_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield _Poster(f"http://127.0.0.1:{httpd.server_port}", slack, clock, service)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def poster() -> Iterator[_Poster]:
    snapshot = _Snapshot(
        settings={"adp": _settings("C0ADP"), "beta": _settings("C0BETA")},
        channels={"C0ADP": "adp", "C0BETA": "beta"},
    )
    yield from _serve(snapshot)


def test_worker_of_a_shared_app_tenant_posts_to_its_latest_bundle_channel(tmp_path, monkeypatch):
    env = tmp_path / "env"
    (env / "tenants" / "adp").mkdir(parents=True)
    (env / "slack-shared-app.yaml").write_text(SHARED_APP_FILE)
    (env / "tenants" / "adp" / "escalation.yaml").write_text(
        "config_version: 1\nchannels: [slack]\nslack:\n  app: shared\n  transport: webhook\n"
        "  channel: C0ADP\n  profile: private\n"
    )
    (env / "tenants" / "adp" / "gates.yaml").write_text(GATES)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    snapshot = server.tenant_slack_settings(hermetic_settings(tenants="adp"))
    snapshot.refresh()

    for poster in _serve(snapshot):
        response = poster.post()

        assert response.status_code == 200
        assert response.json() == {"ts": TS}
        assert poster.slack.posts == [
            {
                "channel": "C0ADP",
                "text": TEXT,
                "blocks": BLOCKS,
                "unfurl_links": False,
                "unfurl_media": False,
            }
        ]


@pytest.mark.parametrize("token", ["forged", None])
def test_a_caller_that_is_not_a_tenant_activity_worker_is_refused(poster, token):
    response = poster.post(token=token)

    assert response.status_code == 401
    assert poster.slack.posts == []


def test_an_unreachable_api_server_answers_503(poster):
    response = poster.post(token="api-unreachable")

    assert response.status_code == 503
    assert poster.slack.posts == []


EXTRA_FIELDS = ("channel", "username", "icon_url", "as_user")


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[]", json.dumps({"blocks": []}).encode(), json.dumps({"text": " "}).encode()]
    + [json.dumps({"text": TEXT, "blocks": {"type": "section"}}).encode()]
    + [json.dumps({"text": TEXT, extra: "C0BETA"}).encode() for extra in EXTRA_FIELDS],
    ids=["not-json", "not-an-object", "no-text", "blank-text", "blocks-not-a-list"]
    + [f"names-{extra}" for extra in EXTRA_FIELDS],
)
def test_a_malformed_body_is_refused_before_slack(poster, content):
    response = httpx.post(
        f"{poster.url}{MESSAGES_PATH}",
        content=content,
        headers={"Authorization": "Bearer adp-worker-SECRET"},
    )

    assert (response.status_code, response.json()["error"]) == (400, "invalid_body")
    assert poster.slack.posts == []


@pytest.mark.parametrize("deadline", ["soon", "nan", "inf", "0.5"])
def test_a_deadline_that_is_not_a_finite_second_or_more_is_refused(poster, deadline):
    response = poster.post(headers={DEADLINE_HEADER: deadline})

    assert (response.status_code, response.json()["error"]) == (400, "invalid_deadline")
    assert poster.slack.posts == []


def test_a_rate_limit_without_retry_after_pauses_every_tenant_for_the_default(poster):
    poster.slack.error = _slack_error("ratelimited", 429)

    limited = poster.post(token="adp-worker-SECRET")
    poster.slack.error = None
    poster.clock.now += server.DEFAULT_PAUSE_SECONDS - 1
    paused = poster.post(token="beta-worker-SECRET")
    poster.clock.now += 1
    resumed = poster.post(token="beta-worker-SECRET")

    assert limited.json() == {"error": "ratelimited", "retry_after": server.DEFAULT_PAUSE_SECONDS}
    assert paused.status_code == 429
    assert resumed.status_code == 200


def test_a_slack_connection_failure_answers_504(poster):
    poster.slack.error = SlackClientError("connection reset")

    response = poster.post()

    assert (response.status_code, response.json()) == (504, {"error": "slack_unreachable"})


def test_a_snapshot_channel_that_is_not_a_channel_id_is_refused():
    snapshot = _Snapshot(settings={"adp": _settings("U0123")}, channels={"U0123": "adp"})
    for poster in _serve(snapshot):
        response = poster.post()

        assert response.status_code == 403
        assert poster.slack.posts == []


def test_a_tenant_whose_latest_bundle_uses_its_own_app_is_refused():
    snapshot = _Snapshot(settings={"adp": _settings("C0ADP", app=OWN_APP)})
    for poster in _serve(snapshot):
        response = poster.post()

        assert response.status_code == 403
        assert poster.slack.posts == []


def test_only_the_tenant_the_index_routes_a_shared_channel_to_posts_to_it():
    snapshot = _Snapshot(
        settings={"adp": _settings("C0ONE"), "beta": _settings("C0ONE")},
        channels={"C0ONE": "adp"},
    )
    for poster in _serve(snapshot):
        refused = poster.post(token="beta-worker-SECRET")
        served = poster.post(token="adp-worker-SECRET")

        assert refused.status_code == 403
        assert served.status_code == 200
        assert [post["channel"] for post in poster.slack.posts] == ["C0ONE"]


def test_a_slack_rate_limit_pauses_posts_of_every_tenant_for_retry_after(poster):
    poster.slack.error = _slack_error("ratelimited", 429, {"Retry-After": "30"})

    limited = poster.post(token="adp-worker-SECRET")
    poster.slack.error = None
    poster.clock.now += 10
    paused = poster.post(token="beta-worker-SECRET")
    poster.clock.now += 21
    resumed = poster.post(token="beta-worker-SECRET")

    assert limited.status_code == 429
    assert limited.json()["error"] == "ratelimited"
    assert paused.status_code == 429
    assert paused.json() == {"error": "ratelimited", "retry_after": 20.0}
    assert paused.headers["Retry-After"] == "20"
    assert resumed.status_code == 200
    assert [post["channel"] for post in poster.slack.posts] == ["C0ADP", "C0BETA"]


def test_a_slack_error_code_is_returned(poster):
    poster.slack.error = _slack_error("not_in_channel")

    response = poster.post()

    assert response.status_code == 502
    assert response.json() == {"error": "not_in_channel"}


def test_a_tenant_is_held_below_one_post_per_second(poster):
    accepted = 0
    while (response := poster.post()).status_code == 200:
        accepted += 1
    posted = len(poster.slack.posts)
    poster.clock.now += 1.0
    within_a_second = poster.post()
    other_tenant = poster.post(token="beta-worker-SECRET")
    poster.clock.now += response.json()["retry_after"]
    after_the_wait = poster.post()

    assert response.status_code == 429
    assert response.json()["retry_after"] > 0
    assert posted == accepted
    assert within_a_second.status_code == 429
    assert other_tenant.status_code == 200
    assert after_the_wait.status_code == 200


def test_a_tenant_is_held_to_a_small_in_flight_cap(poster):
    poster.slack.release = threading.Event()
    results: list[httpx.Response] = []
    workers = [threading.Thread(target=lambda: results.append(poster.post())) for _ in range(2)]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert poster.slack.entered.acquire(timeout=5)
    poster.clock.now += 60
    while_in_flight = poster.post()
    poster.slack.release.set()
    for worker in workers:
        worker.join(5)

    assert while_in_flight.status_code == 429
    assert while_in_flight.json()["error"] == "too_many_in_flight"
    assert [result.status_code for result in results] == [200, 200]


def test_a_deadline_above_the_server_maximum_is_clamped_to_it(poster):
    poster.post(headers={DEADLINE_HEADER: "600"})
    poster.clock.now += 60
    poster.post(headers={DEADLINE_HEADER: "2.5"})

    assert poster.slack.timeouts[-2:] == [int(server.MAX_DEADLINE_SECONDS), 2]


def test_no_log_event_carries_the_request_body_or_the_authorization_header(poster, caplog):
    with structlog.testing.capture_logs() as events:
        poster.post()
        poster.post(body={"text": TEXT, "channel": "C0BETA"})
        poster.clock.now += 60
        poster.slack.error = _slack_error("not_in_channel")
        poster.post()

    logged = repr(events) + caplog.text
    assert [event["event"] for event in events] == [
        "slack_post_delivered",
        "slack_post_refused",
        "slack_post_failed",
    ]
    assert all(event["tenant"] == "adp" and event["channel"] == "C0ADP" for event in events)
    assert "SECRET" not in logged


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"ok": True, "team_id": "T0OTHER"}, id="other-team"),
        pytest.param(
            {"ok": True, "team_id": ORGANIZATION, "enterprise_id": "E0OTHER"}, id="other-org"
        ),
        pytest.param(_slack_error("invalid_auth"), id="token-rejected"),
    ],
)
def test_readiness_fails_unless_auth_test_names_the_shared_app_organization(identity):
    slack = _Slack()
    slack.identity = identity
    for poster in _serve(_Snapshot(), slack):
        assert poster.get("/ready").status_code == 503
        assert poster.post().status_code == 503
        assert poster.get("/health").status_code == 200


def test_readiness_passes_when_auth_test_names_the_shared_app_organization(poster):
    assert poster.get("/ready").status_code == 200


def test_readiness_passes_for_a_workspace_whose_team_id_is_the_organization():
    slack = _Slack()
    slack.identity = {"ok": True, "team_id": ORGANIZATION}
    for poster in _serve(
        _Snapshot(settings={"adp": _settings("C0ADP")}, channels={"C0ADP": "adp"}), slack
    ):
        assert poster.get("/ready").status_code == 200
        assert poster.post().status_code == 200


def test_a_slack_outage_at_startup_heals_once_auth_test_passes():
    class _Recovering(_Slack):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 2

        def auth_test(self) -> Any:
            if self.failures:
                self.failures -= 1
                raise SlackClientError("connection reset")
            return super().auth_test()

    slack = _Recovering()
    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp",)),
        snapshot=_Snapshot(),
        slack_client=slack.client,
    )

    asyncio.run(server.await_slack(service, interval=0))

    assert service.ready
    assert slack.failures == 0


def test_the_slack_call_gets_the_deadline_less_the_token_review_time():
    clock = _Clock()

    class _SlowReview:
        def __init__(self, seconds: float) -> None:
            self.seconds = seconds

        def tenant(self, token: str) -> str:
            clock.now += self.seconds
            return "adp"

    slack = _Slack()
    snapshot = _Snapshot(settings={"adp": _settings("C0ADP")}, channels={"C0ADP": "adp"})
    message = json.dumps({"text": TEXT}).encode()
    review = _SlowReview(0)
    service = server.PostingService(
        identity=review, snapshot=snapshot, slack_client=slack.client, clock=clock
    )
    service.check_slack()
    slack.timeouts.clear()

    def _post(review_seconds: float) -> server.Reply:
        review.seconds = review_seconds
        return service.post(authorization="Bearer adp-worker-SECRET", deadline="5", body=message)

    assert _post(2.0).status == 200
    late = _post(4.5)

    assert slack.timeouts == [3]
    assert (late.status, late.body["error"]) == (504, "deadline_exceeded")
    assert len(slack.posts) == 1


def test_a_one_second_deadline_is_refused_once_the_token_review_takes_any_of_it():
    clock = _Clock()

    class _Review:
        def tenant(self, token: str) -> str:
            clock.now += 0.05
            return "adp"

    slack = _Slack()
    snapshot = _Snapshot(settings={"adp": _settings("C0ADP")}, channels={"C0ADP": "adp"})
    service = server.PostingService(
        identity=_Review(), snapshot=snapshot, slack_client=slack.client, clock=clock
    )
    service.check_slack()

    reply = service.post(
        authorization="Bearer adp-worker-SECRET",
        deadline="1",
        body=json.dumps({"text": TEXT}).encode(),
    )

    assert (reply.status, reply.body["error"]) == (504, "deadline_exceeded")
    assert slack.posts == []


def test_a_failed_first_refresh_serves_health_and_is_ready_after_a_good_refresh(
    tmp_path, monkeypatch
):
    env = tmp_path / "env"
    (env / "tenants" / "adp").mkdir(parents=True)
    shared_app = env / "slack-shared-app.yaml"
    shared_app.write_text("config_version: 1\napp_id: [\n")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    snapshot = server.tenant_slack_settings(hermetic_settings(tenants="adp"))
    snapshot.refresh()

    for poster in _serve(snapshot):
        assert poster.get("/health").status_code == 200
        assert poster.get("/ready").status_code == 503

        shared_app.write_text(SHARED_APP_FILE)
        snapshot.refresh()
        poster.service.check_slack()

        assert poster.get("/ready").status_code == 200


def test_a_changed_shared_app_is_not_ready_until_auth_test_passes_for_it():
    snapshot = _Snapshot()
    slack = _Slack()
    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp",)), snapshot=snapshot, slack_client=slack.client
    )
    service.check_slack()
    assert service.ready

    snapshot.app = SharedSlackApp("A0SHARED002", "E0OTHER", "https://slack.com/api/")

    assert not service.ready
    service.check_slack()
    assert not service.ready


def test_two_posts_with_one_deadline_reuse_one_web_client():
    clients = server.SlackClients("xoxb-shared")

    assert clients(SHARED, 3) is clients(SHARED, 3)
    assert clients(SHARED, 3) is not clients(SHARED, 4)


def test_the_posting_service_with_no_served_tenant_exits_naming_tenant_and_tenants(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: hermetic_settings(tenant="", tenants=""))

    with pytest.raises(SystemExit, match="neither TENANT nor TENANTS"):
        server.main()


def _leaf(tmp_path, days: int) -> tuple[Any, Any]:
    ca_key, ca = new_ca()
    certificate, key = issue_leaf(ca_key, ca, ["127.0.0.1"], validity=timedelta(days=days))
    (tmp_path / "leaf.pem").write_text(certificate)
    (tmp_path / "leaf.key").write_text(key)
    return tmp_path / "leaf.pem", tmp_path / "leaf.key"


@pytest.mark.parametrize(("days", "logged"), [(20, True), (60, False)])
def test_a_served_leaf_within_30_days_of_its_expiry_is_logged(tmp_path, days, logged):
    leaf, _ = _leaf(tmp_path, days)

    with structlog.testing.capture_logs() as logs:
        server.check_leaf(expires_at(leaf), leaf)

    events = [entry["event"] for entry in logs]
    assert ("slack_poster_leaf_expiring" in events) is logged
    assert "slack_poster_leaf_renewed" not in events


def test_a_renewed_leaf_on_disk_does_not_hide_the_expiring_served_one(tmp_path):
    leaf, _ = _leaf(tmp_path, 20)
    served = expires_at(leaf)
    _leaf(tmp_path, 400)

    with structlog.testing.capture_logs() as logs:
        server.check_leaf(served, leaf)

    assert [entry["event"] for entry in logs] == [
        "slack_poster_leaf_expiring",
        "slack_poster_leaf_renewed",
    ]


def test_a_slack_call_past_the_deadline_answers_504_without_waiting_for_slack():
    slack = _Slack()
    slack.release = threading.Event()
    snapshot = _Snapshot(settings={"adp": _settings("C0ADP")}, channels={"C0ADP": "adp"})
    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp",)), snapshot=snapshot, slack_client=slack.client
    )
    service.check_slack()
    started = time.monotonic()

    reply = service.post(
        authorization="Bearer adp-worker-SECRET",
        deadline="2",
        body=json.dumps({"text": TEXT}).encode(),
    )

    elapsed = time.monotonic() - started
    slack.release.set()
    assert (reply.status, reply.body["error"]) == (504, "deadline_exceeded")
    assert elapsed < 2


async def test_the_readiness_watch_survives_an_error_and_rechecks_a_changed_shared_app():
    snapshot = _Snapshot()
    slack = _Slack()
    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp",)), snapshot=snapshot, slack_client=slack.client
    )

    async def until(condition) -> bool:
        for _ in range(200):
            if condition():
                return True
            await asyncio.sleep(0.01)
        return False

    with structlog.testing.capture_logs() as logs:
        watch = asyncio.create_task(server.watch_slack(service, interval=0.01))
        assert await until(lambda: service.ready)
        slack.identity = RuntimeError("an unexpected failure")
        snapshot.app = SharedSlackApp("A0SHARED002", ORGANIZATION, "https://slack.com/api/")
        assert await until(
            lambda: any(entry["event"] == "slack_readiness_check_failed" for entry in logs)
        )
        slack.identity = {"ok": True, "team_id": "T0WORKSPACE", "enterprise_id": ORGANIZATION}
        recovered = await until(lambda: service.ready)
        watch.cancel()

    assert recovered


def test_the_posting_service_listens_before_its_first_refresh_finishes(tmp_path, monkeypatch):
    certificate, key = _leaf(tmp_path, 400)
    settings = hermetic_settings(
        tenants="adp",
        server_tls=True,
        server_tls_certificate_file=str(certificate),
        server_tls_key_file=str(key),
    )
    refreshing = threading.Event()

    class _SlowSnapshot(_Snapshot):
        async def refresh_then_start(self) -> None:
            refreshing.set()
            await asyncio.sleep(2)
            raise TimeoutError("the first refresh hung")

    class _Listening(Exception):
        pass

    def serve_forever(self) -> None:
        self.server_close()
        assert refreshing.wait(5)
        raise _Listening

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "tenant_slack_settings", lambda *args: _SlowSnapshot())
    monkeypatch.setattr(server.KubernetesTokenReview, "in_cluster", staticmethod(lambda: None))
    monkeypatch.setattr(server, "PORT", 0)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve_forever)

    with pytest.raises(_Listening):
        server.main()


def test_the_posting_service_without_a_mounted_certificate_exits_naming_the_settings(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: hermetic_settings(tenants="adp"))

    with pytest.raises(SystemExit, match="SERVER_TLS_CERTIFICATE_FILE"):
        server.main()
