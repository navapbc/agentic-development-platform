"""The posting service posts a shared-app tenant's alert to that tenant's channel only."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing
from scripts.slack_poster_tls import issue_leaf, new_ca
from services.integration.slack.approver_groups import GROUP_REFRESH_SECONDS
from services.integration.slack.shared_app import SharedSlackApp
from services.orchestration.escalation import (
    MAX_APPROVER_GROUPS,
    OWN_APP,
    SHARED_APP,
    Approvers,
    SlackGroup,
    SlackSettings,
)
from services.orchestration.platform_escalation import AdminDestination
from services.server_tls import expires_at
from services.slack_poster import (
    DEADLINE_HEADER,
    GROUPS_PATH,
    MESSAGES_PATH,
    SERVICE_ACCOUNT_AUDIENCE,
    UPDATE_PATH,
    server,
)
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
        "audiences": [SERVICE_ACCOUNT_AUDIENCE],
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
        self.group_reads: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []
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
        self.sent.append(("chat.postMessage", kwargs["channel"]))
        self.entered.release()
        if self.release is not None:
            self.release.wait(5)
        if self.error is not None:
            raise self.error
        return {"ok": True, "channel": kwargs["channel"], "ts": TS}

    def chat_update(self, **kwargs: Any) -> Any:
        self.updates.append(kwargs)
        self.sent.append(("chat.update", kwargs["channel"]))
        return {"ok": True, "channel": kwargs["channel"], "ts": kwargs["ts"]}

    def usergroups_users_list(self, **kwargs: Any) -> Any:
        self.group_reads.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ok": True, "users": ["U0MEMBER"]}


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

    def read_group(self, group_id: str, token: str | None = "adp-worker-SECRET") -> httpx.Response:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return httpx.get(f"{self.url}{GROUPS_PATH}{group_id}", headers=headers)


def _serve(
    snapshot: Any,
    slack: _Slack | None = None,
    admin: AdminDestination | None = None,
    hold: Callable[[threading.Condition, float], object] | None = None,
) -> Iterator[_Poster]:
    slack = slack or _Slack()
    clock = _Clock()

    def advance(condition: threading.Condition, seconds: float) -> None:
        clock.now += seconds

    service = server.PostingService(
        identity=WorkerIdentity(_review, ("adp", "beta")),
        snapshot=snapshot,
        slack_client=slack.client,
        owner_key=OWNER_KEY,
        clock=clock,
        admin=lambda: admin,
        hold=hold or advance,
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


GROUP = "S0123456789"


def test_a_worker_reads_the_members_of_a_group_its_latest_bundle_names(tmp_path, monkeypatch):
    env = tmp_path / "env"
    (env / "tenants" / "adp").mkdir(parents=True)
    (env / "slack-shared-app.yaml").write_text(SHARED_APP_FILE)
    (env / "tenants" / "adp" / "escalation.yaml").write_text(
        "config_version: 1\nchannels: [slack]\nslack:\n  app: shared\n  transport: webhook\n"
        "  channel: C0ADP\n  profile: private\n  allowlist:\n    gate: channel\n"
        f"    repair:\n      - group_id: {GROUP}\n"
    )
    (env / "tenants" / "adp" / "gates.yaml").write_text(GATES)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    snapshot = server.tenant_slack_settings(hermetic_settings(tenants="adp"))
    snapshot.refresh()

    for poster in _serve(snapshot):
        response = poster.read_group(GROUP)

        assert (response.status_code, response.json()) == (200, {"members": ["U0MEMBER"]})
        assert poster.slack.group_reads == [{"usergroup": GROUP}]


def _group_settings(channel: str, *group_ids: str, team_id: str = "") -> SlackSettings:
    return dataclasses.replace(
        _settings(channel),
        allowlist={
            "repair": Approvers(
                groups=tuple(SlackGroup(ORGANIZATION, group_id, team_id) for group_id in group_ids)
            )
        },
    )


def test_a_group_read_sends_the_team_id_the_entry_sets():
    snapshot = _Snapshot(settings={"adp": _group_settings("C0ADP", GROUP, team_id="T0WORKSPACE")})
    for poster in _serve(snapshot):
        response = poster.read_group(GROUP)

        assert response.status_code == 200
        assert poster.slack.group_reads == [{"usergroup": GROUP, "team_id": "T0WORKSPACE"}]


def test_a_tenant_at_the_group_limit_passes_the_bucket_in_every_refresh_cycle():
    group_ids = [f"S{index:010d}" for index in range(MAX_APPROVER_GROUPS)]
    snapshot = _Snapshot(settings={"adp": _group_settings("C0ADP", *group_ids)})
    for poster in _serve(snapshot):
        first = [poster.read_group(group_id).status_code for group_id in group_ids]
        poster.clock.now += GROUP_REFRESH_SECONDS
        second = [poster.read_group(group_id).status_code for group_id in group_ids]

        assert first == second == [200] * MAX_APPROVER_GROUPS


def test_a_group_read_for_a_group_the_tenant_does_not_name_is_refused():
    snapshot = _Snapshot(
        settings={"adp": _group_settings("C0ADP"), "beta": _group_settings("C0BETA", GROUP)},
        channels={"C0ADP": "adp", "C0BETA": "beta"},
    )
    for poster in _serve(snapshot):
        refused = poster.read_group(GROUP, token="adp-worker-SECRET")
        served = poster.read_group(GROUP, token="beta-worker-SECRET")

        assert (refused.status_code, refused.json()) == (403, {"error": "group_not_named"})
        assert served.status_code == 200
        assert len(poster.slack.group_reads) == 1


@pytest.mark.parametrize("token", ["forged", None])
def test_a_group_read_by_a_caller_that_is_not_a_tenant_activity_worker_is_refused(token):
    snapshot = _Snapshot(settings={"adp": _group_settings("C0ADP", GROUP)})
    for poster in _serve(snapshot):
        response = poster.read_group(GROUP, token=token)

        assert (response.status_code, response.json()) == (401, {"error": "unauthenticated"})
        assert poster.slack.group_reads == []


def test_a_group_read_returns_the_slack_error_code():
    snapshot = _Snapshot(settings={"adp": _group_settings("C0ADP", GROUP)})
    for poster in _serve(snapshot):
        poster.slack.error = _slack_error("missing_scope")

        response = poster.read_group(GROUP)

        assert (response.status_code, response.json()) == (502, {"error": "missing_scope"})


ADMIN_CHANNEL = "C0ADMIN"
OWNER_KEY = b"posting service owner key"


def _update(poster: _Poster, token: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{poster.url}{UPDATE_PATH}",
        content=json.dumps(body),
        headers={"Authorization": f"Bearer {token}"},
    )


def _admin_poster() -> Iterator[_Poster]:
    snapshot = _Snapshot(
        settings={"adp": _settings("C0ADP"), "beta": _settings("C0BETA")},
        channels={"C0ADP": "adp", "C0BETA": "beta"},
    )
    yield from _serve(snapshot, admin=AdminDestination(slack_channel=ADMIN_CHANNEL))


def test_a_tenant_posts_updates_and_replies_to_its_own_admin_post_only():
    for poster in _admin_poster():
        posted = poster.post(body={"text": TEXT, "audience": "admin"})
        owner = posted.json()["owner_token"]
        reply = poster.post(
            body={"text": TEXT, "audience": "admin", "thread_ts": TS, "owner_token": owner}
        )
        poster.clock.now += 60
        update = _update(
            poster,
            "adp-worker-SECRET",
            {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": owner},
        )
        foreign = poster.post(
            token="beta-worker-SECRET",
            body={"text": TEXT, "audience": "admin", "thread_ts": TS, "owner_token": owner},
        )

        assert [posted.status_code, reply.status_code, update.status_code] == [200, 200, 200]
        assert [post["channel"] for post in poster.slack.posts] == [ADMIN_CHANNEL] * 2
        assert poster.slack.posts[1]["thread_ts"] == TS
        assert "owner_token" not in reply.json()
        assert [(edit["channel"], edit["ts"]) for edit in poster.slack.updates] == [
            (ADMIN_CHANNEL, TS)
        ]
        assert (foreign.status_code, foreign.json()) == (403, {"error": "post_not_owned"})


def test_an_admin_post_update_after_a_posting_service_restart_is_served_with_its_token():
    for before in _admin_poster():
        owner = before.post(body={"text": TEXT, "audience": "admin"}).json()["owner_token"]
    for after in _admin_poster():
        update = _update(
            after,
            "adp-worker-SECRET",
            {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": owner},
        )
        reply = after.post(
            body={"text": TEXT, "audience": "admin", "thread_ts": TS, "owner_token": owner}
        )

        assert [update.status_code, reply.status_code] == [200, 200]
        assert [(edit["channel"], edit["ts"]) for edit in after.slack.updates] == [
            (ADMIN_CHANNEL, TS)
        ]


@pytest.mark.parametrize("owner_token", [None, "forged"])
def test_an_admin_post_edit_without_its_owner_token_is_refused(owner_token):
    for poster in _admin_poster():
        poster.post(body={"text": TEXT, "audience": "admin"})
        body = {"text": TEXT, "audience": "admin", "ts": TS}
        if owner_token is not None:
            body["owner_token"] = owner_token

        update = _update(poster, "adp-worker-SECRET", body)

        assert (update.status_code, update.json()) == (403, {"error": "post_not_owned"})
        assert poster.slack.updates == []


def test_an_owner_token_of_another_key_is_refused():
    other = server.PostingService(
        identity=WorkerIdentity(_review, ("adp",)),
        snapshot=_Snapshot(),
        slack_client=_Slack().client,
        owner_key=b"another key",
    )
    for poster in _admin_poster():
        update = _update(
            poster,
            "adp-worker-SECRET",
            {
                "text": TEXT,
                "audience": "admin",
                "ts": TS,
                "owner_token": other.owner_token(ADMIN_CHANNEL, TS, "adp"),
            },
        )

        assert (update.status_code, update.json()) == (403, {"error": "post_not_owned"})


def test_an_admin_post_without_an_admin_channel_is_refused(poster):
    response = poster.post(body={"text": TEXT, "audience": "admin"})

    assert (response.status_code, response.json()) == (403, {"error": "no_admin_channel"})
    assert poster.slack.posts == []


def _platform_status(account: str, pod: str) -> dict[str, Any]:
    status = _status("tenant-adp")
    status["user"] = {
        "username": f"system:serviceaccount:agentic-dev-platform:{account}",
        "extra": {
            "authentication.kubernetes.io/pod-name": [pod],
            "authentication.kubernetes.io/pod-uid": ["0b6c3f1e-pod"],
        },
    }
    return status


REVIEWS["webhook-SECRET"] = _platform_status("inbound-webhook", "inbound-webhook-7f9c-abc")
REVIEWS["validate-SECRET"] = _platform_status("deployment-validate", "deployment-validate-29-x")
REVIEWS["borrowed-SECRET"] = _platform_status("inbound-webhook", "activity-worker-abc")
REVIEWS["other-account-SECRET"] = _platform_status("litellm", "litellm-abc")
ADMIN_GROUP = "S0ADMINGRP1"


def _admin_destination() -> AdminDestination:
    return AdminDestination(
        slack_channel=ADMIN_CHANNEL,
        allowlist={"repair": Approvers(groups=(SlackGroup(ORGANIZATION, ADMIN_GROUP, ""),))},
    )


def _platform_poster() -> Iterator[_Poster]:
    snapshot = _Snapshot(
        settings={"adp": _group_settings("C0ADP", GROUP)}, channels={"C0ADP": "adp"}
    )
    yield from _serve(snapshot, admin=_admin_destination())


@pytest.mark.parametrize("token", ["webhook-SECRET", "validate-SECRET"])
def test_the_platform_caller_posts_updates_and_replies_only_in_the_admin_channel(token):
    for poster in _platform_poster():
        posted = poster.post(token=token, body={"text": TEXT, "audience": "admin"})
        owner = posted.json()["owner_token"]
        reply = poster.post(
            token=token,
            body={"text": TEXT, "audience": "admin", "thread_ts": TS, "owner_token": owner},
        )
        poster.clock.now += 60
        update = _update(
            poster, token, {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": owner}
        )
        tenant_channel = poster.post(token=token, body={"text": TEXT})

        assert [posted.status_code, reply.status_code, update.status_code] == [200, 200, 200]
        assert [post["channel"] for post in poster.slack.posts] == [ADMIN_CHANNEL] * 2
        assert [(edit["channel"], edit["ts"]) for edit in poster.slack.updates] == [
            (ADMIN_CHANNEL, TS)
        ]
        assert (tenant_channel.status_code, tenant_channel.json()) == (
            403,
            {"error": "platform_caller_not_admin"},
        )


def test_a_tenant_cannot_edit_or_reply_to_a_platform_caller_post_with_its_token():
    for poster in _platform_poster():
        owner = poster.post(token="webhook-SECRET", body={"text": TEXT, "audience": "admin"})
        platform_token = owner.json()["owner_token"]
        tenant_post = poster.post(body={"text": TEXT, "audience": "admin"})
        tenant_token = tenant_post.json()["owner_token"]

        update = _update(
            poster,
            "adp-worker-SECRET",
            {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": platform_token},
        )
        reply = poster.post(
            body={
                "text": TEXT,
                "audience": "admin",
                "thread_ts": TS,
                "owner_token": platform_token,
            }
        )
        platform_edit = _update(
            poster,
            "webhook-SECRET",
            {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": tenant_token},
        )

        assert platform_token != tenant_token
        assert (update.status_code, update.json()) == (403, {"error": "post_not_owned"})
        assert (reply.status_code, reply.json()) == (403, {"error": "post_not_owned"})
        assert (platform_edit.status_code, platform_edit.json()) == (
            403,
            {"error": "post_not_owned"},
        )
        assert poster.slack.updates == []
        assert len(poster.slack.posts) == 2


def test_the_platform_caller_reads_only_the_groups_the_admin_allowlist_names():
    for poster in _platform_poster():
        admin_group = poster.read_group(ADMIN_GROUP, token="webhook-SECRET")
        tenant_group = poster.read_group(GROUP, token="webhook-SECRET")
        tenant_reads_admin_group = poster.read_group(ADMIN_GROUP, token="adp-worker-SECRET")

        assert (admin_group.status_code, admin_group.json()) == (200, {"members": ["U0MEMBER"]})
        assert (tenant_group.status_code, tenant_group.json()) == (
            403,
            {"error": "group_not_named"},
        )
        assert tenant_reads_admin_group.status_code == 403
        assert [read["usergroup"] for read in poster.slack.group_reads] == [ADMIN_GROUP]


@pytest.mark.parametrize("token", ["borrowed-SECRET", "other-account-SECRET"])
def test_a_platform_token_from_another_pod_or_service_account_is_refused(token):
    for poster in _platform_poster():
        posted = poster.post(token=token, body={"text": TEXT, "audience": "admin"})
        read = poster.read_group(ADMIN_GROUP, token=token)

        assert (posted.status_code, read.status_code) == (401, 401)
        assert (poster.slack.posts, poster.slack.group_reads) == ([], [])


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


def test_two_replies_then_two_edits_within_a_second_are_delivered_in_order():
    for poster in _admin_poster():
        poster.post()
        owner = poster.post(body={"text": TEXT, "audience": "admin"}).json()["owner_token"]
        poster.clock.now += 60
        poster.slack.sent.clear()
        start = poster.clock.now
        admin_reply = {"text": TEXT, "audience": "admin", "thread_ts": TS, "owner_token": owner}
        admin_edit = {"text": TEXT, "audience": "admin", "ts": TS, "owner_token": owner}
        responses = [
            poster.post(body={"text": TEXT, "thread_ts": TS}),
            poster.post(body=admin_reply),
            _update(poster, "adp-worker-SECRET", {"text": TEXT, "ts": TS}),
            _update(poster, "adp-worker-SECRET", admin_edit),
        ]

        assert [response.status_code for response in responses] == [200] * 4
        assert poster.slack.sent == [
            ("chat.postMessage", "C0ADP"),
            ("chat.postMessage", ADMIN_CHANNEL),
            ("chat.update", "C0ADP"),
            ("chat.update", ADMIN_CHANNEL),
        ]
        assert poster.clock.now - start < 1


def test_two_tenants_together_edit_no_faster_than_the_app_wide_edit_rate(poster):
    edit = {"text": TEXT, "ts": TS}
    burst = [_update(poster, "adp-worker-SECRET", edit) for _ in range(server.EDIT_BURST)]
    start = poster.clock.now
    other_tenant = _update(poster, "beta-worker-SECRET", edit)

    assert [response.status_code for response in burst] == [200] * server.EDIT_BURST
    assert other_tenant.status_code == 200
    assert poster.clock.now - start == pytest.approx(1 / server.EDITS_PER_SECOND)
    assert [update["channel"] for update in poster.slack.updates][-1] == "C0BETA"


def test_a_call_without_a_token_is_held_until_a_refill_or_refused_at_its_deadline(poster):
    burst = [poster.post() for _ in range(server.POST_BURST)]
    start = poster.clock.now
    held = poster.post()
    held_for = poster.clock.now - start
    refused = poster.post(headers={DEADLINE_HEADER: "2"})
    start = poster.clock.now
    other_tenant = poster.post(token="beta-worker-SECRET")

    assert [response.status_code for response in [*burst, held]] == [200] * 3
    assert held_for == 1 / server.POSTS_PER_SECOND
    assert refused.status_code == 429
    assert refused.json() == {"error": "channel_rate_limited", "retry_after": 2.0}
    assert refused.headers["Retry-After"] == "2"
    assert len(poster.slack.posts) == 4
    assert other_tenant.status_code == 200
    assert poster.clock.now == start


def _wait_until(condition: Callable[[], object]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_tenant_holding_a_call_queues_no_more_ahead_of_another_tenant():
    snapshot = _Snapshot(
        settings={"adp": _settings("C0ADP"), "beta": _settings("C0BETA")},
        channels={"C0ADP": "adp", "C0BETA": "beta"},
    )
    admin = AdminDestination(slack_channel=ADMIN_CHANNEL)
    for poster in _serve(snapshot, admin=admin, hold=lambda condition, _: condition.wait(0.01)):
        _assert_one_held_call_per_tenant(poster)


def _assert_one_held_call_per_tenant(poster: _Poster) -> None:
    body = {"text": TEXT, "audience": "admin"}
    for _ in range(server.POST_BURST):
        assert poster.post(body=body).status_code == 200
    results: dict[str, httpx.Response] = {}

    def post(tenant: str) -> None:
        results[tenant] = poster.post(token=f"{tenant}-worker-SECRET", body=body)

    held = threading.Thread(target=post, args=("adp",))
    held.start()
    _wait_until(lambda: poster.service._held.get("adp"))
    beyond_the_cap = poster.post(body=body)
    other_tenant = threading.Thread(target=post, args=("beta",))
    other_tenant.start()
    _wait_until(lambda: poster.service._held.get("beta"))
    for delivered in (server.POST_BURST + 1, server.POST_BURST + 2):
        poster.clock.now += 1 / server.POSTS_PER_SECOND
        _wait_until(lambda delivered=delivered: len(poster.slack.posts) == delivered)
    held.join(5)
    other_tenant.join(5)

    assert beyond_the_cap.status_code == 429
    assert beyond_the_cap.json()["error"] == "too_many_held"
    assert "Retry-After" in beyond_the_cap.headers
    assert [results["adp"].status_code, results["beta"].status_code] == [200, 200]


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
        owner_key=OWNER_KEY,
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
        identity=review,
        snapshot=snapshot,
        slack_client=slack.client,
        clock=clock,
        owner_key=OWNER_KEY,
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
        identity=_Review(),
        snapshot=snapshot,
        slack_client=slack.client,
        clock=clock,
        owner_key=OWNER_KEY,
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
        identity=WorkerIdentity(_review, ("adp",)),
        snapshot=snapshot,
        slack_client=slack.client,
        owner_key=OWNER_KEY,
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
        identity=WorkerIdentity(_review, ("adp",)),
        snapshot=snapshot,
        slack_client=slack.client,
        owner_key=OWNER_KEY,
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
        identity=WorkerIdentity(_review, ("adp",)),
        snapshot=snapshot,
        slack_client=slack.client,
        owner_key=OWNER_KEY,
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
    monkeypatch.setattr(server, "platform_escalation_preflight", lambda: None)
    monkeypatch.setattr(server.KubernetesTokenReview, "in_cluster", staticmethod(lambda: None))
    monkeypatch.setattr(server, "PORT", 0)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve_forever)

    with pytest.raises(_Listening):
        server.main()


def test_the_posting_service_without_a_mounted_certificate_exits_naming_the_settings(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: hermetic_settings(tenants="adp"))

    with pytest.raises(SystemExit, match="SERVER_TLS_CERTIFICATE_FILE"):
        server.main()


def test_the_posting_service_refuses_an_administrator_channel_without_the_shared_app(
    tmp_path, monkeypatch
):
    from services.orchestration import platform_escalation

    certificate, key = _leaf(tmp_path, 400)
    settings = hermetic_settings(
        tenants="adp",
        server_tls=True,
        server_tls_certificate_file=str(certificate),
        server_tls_key_file=str(key),
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / platform_escalation.PLATFORM_ESCALATION_FILE).write_text(
        "config_version: 1\nadministrators:\n  slack:\n    channel: C0ADMIN\n"
    )
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(config))
    monkeypatch.setattr(platform_escalation, "process_shared_app", lambda: None)
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with pytest.raises(SystemExit, match="no slack-shared-app.yaml"):
        server.main()
