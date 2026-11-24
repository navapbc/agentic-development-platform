"""Unit tests for the human-intervention alert fan-out."""

from __future__ import annotations

import logging
import time
from dataclasses import replace

import httpx
import pytest
from scripts.config import Settings
from services.operator_remedy import NEXT_STEP_PREFIX
from services.orchestration import notifications, remedies
from services.orchestration.notifications import (
    HumanInterventionAlert,
    render_remedy,
    send_human_intervention_alert,
    send_intake_unroutable_alert,
)
from tests.support import SHARED_APP_YAML, configure_tenant, hermetic_settings, tenant_dir


@pytest.fixture(autouse=True)
def _no_environment_files(monkeypatch, tmp_path_factory) -> None:
    configure_tenant(monkeypatch, tmp_path_factory.mktemp("environment"))


def _alert() -> HumanInterventionAlert:
    return HumanInterventionAlert(
        task_id="T-1",
        execution_id="exec-1",
        workflow_id="wf-1",
        repo="agentic-demo-service",
        issue_key="T-1",
        reason="repair_exhausted",
        stage="repair",
        detail="AssertionError: boom",
    )


def test_no_channels_configured_delivers_nothing():
    settings = hermetic_settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    assert send_human_intervention_alert(_alert(), settings) == []


def test_slack_channel_posts_webhook(monkeypatch):
    calls: list[object] = []

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.orchestration.notifications.httpx.post", fake_post)
    settings = hermetic_settings(
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
        slack_webhook_url="https://hook",
    )

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["slack"]
    assert len(calls) == 1


def test_slack_channel_refuses_a_non_https_webhook(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        "services.orchestration.notifications.httpx.post",
        lambda *args, **kwargs: calls.append(args),
    )
    settings = hermetic_settings(
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
        slack_webhook_url="file:///etc/passwd",
    )

    assert send_human_intervention_alert(_alert(), settings) == []
    assert calls == []


def test_email_backup_sends_via_smtp(monkeypatch):
    sent: list[str] = []

    class _SMTP:
        def __init__(self, host, port, timeout=0):
            sent.append(f"{host}:{port}")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, message):
            sent.append(message["To"])

    monkeypatch.setattr("services.orchestration.notifications.smtplib.SMTP", _SMTP)
    settings = hermetic_settings(
        slack_webhook_url="",
        smtp_host="mail.local",
        smtp_port=25,
        alert_email_from="agent@example.com",
        alert_email_to="oncall@example.com",
    )

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["email"]
    assert "mail.local:25" in sent
    assert "oncall@example.com" in sent


def test_jira_channel_posts_comment(monkeypatch):
    comments: list[tuple[str, str]] = []

    class _Adapter:
        def add_comment(self, issue_key, body):
            comments.append((issue_key, body))

    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key, settings=None: _Adapter(),
    )
    settings = hermetic_settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["jira"]
    assert comments[0][0] == "T-1"


def test_channel_error_is_swallowed(monkeypatch):
    def boom(issue_key, settings=None):
        raise RuntimeError("no jira")

    monkeypatch.setattr("services.integration.tickets.build_ticket_backend_for_issue", boom)
    settings = hermetic_settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    assert send_human_intervention_alert(_alert(), settings) == []


def _unconfigured() -> Settings:
    return hermetic_settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )


def _slack_only() -> Settings:
    return hermetic_settings(
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )


def _record_slack_posts(monkeypatch) -> list[dict[str, str]]:
    posted: list[dict[str, str]] = []

    def fake_post(url, *, json, timeout):
        posted.append(json)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    return posted


def test_the_remedy_renders_as_a_next_step_line(monkeypatch):
    posted = _record_slack_posts(monkeypatch)
    alert = replace(_alert(), remedy="Run `inv repair --issue ADSER-24 --retry`.")

    send_human_intervention_alert(alert, _slack_only())

    assert posted[0]["text"].endswith(
        f"{NEXT_STEP_PREFIX} Run `inv repair --issue ADSER-24 --retry`."
    )


def test_an_alert_with_no_remedy_renders_no_next_step_line(monkeypatch):
    posted = _record_slack_posts(monkeypatch)

    send_human_intervention_alert(_alert(), _slack_only())

    assert NEXT_STEP_PREFIX not in posted[0]["text"]


def test_an_alert_that_reaches_no_channel_is_logged_as_an_error(caplog):
    with caplog.at_level(logging.ERROR, logger="services.orchestration.notifications"):
        delivered = send_human_intervention_alert(_alert(), _unconfigured())

    assert delivered == []
    assert "reached no channel" in caplog.text
    assert "SLACK_WEBHOOK_URL" in caplog.text


def test_an_alert_whose_every_channel_raises_is_logged_as_an_error(monkeypatch, caplog):
    def boom(issue_key, settings=None):
        raise RuntimeError("no jira")

    monkeypatch.setattr("services.integration.tickets.build_ticket_backend_for_issue", boom)

    with caplog.at_level(logging.ERROR, logger="services.orchestration.notifications"):
        delivered = send_human_intervention_alert(_alert(), _unconfigured())

    assert delivered == []
    assert "reached no channel" in caplog.text


def test_the_unroutable_intake_alert_renders_its_remedy(monkeypatch):
    posted = _record_slack_posts(monkeypatch)

    send_intake_unroutable_alert(
        issue_key="ADSER-24",
        project="ADSER",
        reason="no allowlisted repository",
        remedy="Add the repository to repositories.yaml.",
        settings=_slack_only(),
    )

    assert f"{NEXT_STEP_PREFIX} Add the repository to repositories.yaml." in posted[0]["text"]


def test_a_host_operator_is_told_the_inv_task_for_the_runs_tenant():
    settings = hermetic_settings(deployment_posture="host", tenant="acme")

    text = render_remedy(remedies.plan_rejected_remedy("ADSER-24"), settings)

    assert "`inv restart --issue ADSER-24 --tenant acme`" in text


def test_a_deployed_operator_is_told_the_kubectl_command_in_the_tenants_namespace():
    settings = hermetic_settings(deployment_posture="deployed", tenant="acme")

    text = render_remedy(remedies.plan_rejected_remedy("ADSER-24"), settings)

    assert (
        "`kubectl -n tenant-acme exec -i deploy/activity-worker -c activity-worker -- "
        "python -m scripts.restart ADSER-24`"
    ) in text
    assert "inv " not in text


@pytest.mark.parametrize(
    ("posture", "location"),
    [
        ("host", "`config/local/tenants/acme/repositories.yaml`"),
        ("deployed", "`tenants/acme/` of the bucket `TENANT_CONFIG_BUCKET` names"),
    ],
)
def test_a_config_key_is_located_where_the_operators_posture_keeps_it(posture, location):
    settings = hermetic_settings(deployment_posture=posture, tenant="acme")
    unlisted = remedies.unroutable_remedy("ADSER-24", "ADSER", ("navapbc/x",))

    assert location in render_remedy(unlisted, settings)


def _every_channel(monkeypatch, tmp_path) -> tuple[Settings, list[str]]:
    reached: list[str] = []

    class _Adapter:
        def add_comment(self, issue_key, body):
            reached.append("jira")

    class _SMTP:
        def __init__(self, host, port, timeout=0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, message):
            reached.append("email")

    def fake_post(url, *, json, timeout):
        reached.append("slack")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(
        "services.integration.tickets.build_ticket_backend_for_issue",
        lambda issue_key, settings=None: _Adapter(),
    )
    monkeypatch.setattr("services.orchestration.notifications.smtplib.SMTP", _SMTP)
    monkeypatch.setattr("services.orchestration.notifications.httpx.post", fake_post)
    configure_tenant(monkeypatch, tmp_path)
    settings = hermetic_settings(
        tenant="",
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="mail.local",
        alert_email_from="agent@example.com",
        alert_email_to="oncall@example.com",
    )
    return settings, reached


def test_a_tenant_without_an_escalation_file_reaches_every_configured_channel(
    monkeypatch, tmp_path
):
    settings, reached = _every_channel(monkeypatch, tmp_path)

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["jira", "email", "slack"]
    assert reached == ["jira", "email", "slack"]


def test_an_escalation_file_without_email_delivers_to_jira_and_slack_only(monkeypatch, tmp_path):
    settings, reached = _every_channel(monkeypatch, tmp_path)
    (tenant_dir(tmp_path) / "escalation.yaml").write_text(
        "config_version: 1\n"
        "channels: [jira, slack]\n"
        "slack:\n"
        "  transport: webhook\n"
        "  channel: C0123456789\n"
        "  organization_id: T0123456789\n"
        "  app_id: A0123456789\n"
        "  profile: private\n"
    )

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["jira", "slack"]
    assert reached == ["jira", "slack"]


def test_an_unusable_escalation_file_still_reaches_every_configured_channel(
    monkeypatch, tmp_path, caplog
):
    settings, reached = _every_channel(monkeypatch, tmp_path)
    (tenant_dir(tmp_path) / "escalation.yaml").write_text("config_version: 1\nchannels: [pager]\n")

    with caplog.at_level(logging.ERROR, logger="services.orchestration.notifications"):
        delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["jira", "email", "slack"]
    assert "escalation.yaml is unusable" in caplog.text


_SLACK_FILE = (
    "config_version: 1\n"
    "slack:\n"
    "  transport: socket\n"
    "  channel: C0123456789\n"
    "  organization_id: T0123456789\n"
    "  app_id: A0123456789\n"
    "  profile: private\n"
    "  api_base_url: {base}\n"
)


class _WebClient:
    built: list[dict] = []
    posts: list[dict] = []
    error: Exception | None = None

    def __init__(self, **kwargs):
        _WebClient.built.append(kwargs)

    def chat_postMessage(self, **kwargs):
        if _WebClient.error is not None:
            raise _WebClient.error
        _WebClient.posts.append(kwargs)


def _bot_channels(monkeypatch, tmp_path, *, base: str = "https://slack.com/api/"):
    settings, reached = _every_channel(monkeypatch, tmp_path)
    (tenant_dir(tmp_path) / "escalation.yaml").write_text(_SLACK_FILE.format(base=base))
    _WebClient.built, _WebClient.posts, _WebClient.error = [], [], None
    monkeypatch.setattr("services.integration.slack.client.WebClient", _WebClient)
    settings = settings.model_copy(
        update={"slack_bot_token": "xoxb-test", "escalation_action_key": "key"}
    )
    return settings, reached


def _keyed_alert() -> HumanInterventionAlert:
    return replace(
        _alert(),
        reason="gate_paused:security-review",
        stage="review",
        structured_remedy=remedies.gate_decision_remedy("T-1", "security-review"),
        park_key="exec-1:review:security-review:1",
    )


def test_the_bot_posts_an_actionable_message_and_the_incoming_webhook_stays_quiet(
    monkeypatch, tmp_path
):
    settings, reached = _bot_channels(monkeypatch, tmp_path)

    delivered = send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)

    assert delivered == ["jira", "email", "slack"]
    assert reached == ["jira", "email"]
    [post] = _WebClient.posts
    assert post["channel"] == "C0123456789"
    assert (post["unfurl_links"], post["unfurl_media"]) == (False, False)
    actions = [block for block in post["blocks"] if block["type"] == "actions"]
    assert [button["action_id"] for button in actions[0]["elements"]] == ["approve", "reject"]
    assert _WebClient.built[0]["retry_handlers"] == []


def test_a_govslack_api_host_receives_the_post(monkeypatch, tmp_path):
    settings, _ = _bot_channels(monkeypatch, tmp_path, base="https://slack-gov.com/api/")

    send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)

    assert _WebClient.built[0]["base_url"] == "https://slack-gov.com/api/"


def test_a_slack_timeout_leaves_slack_out_while_jira_and_email_deliver(monkeypatch, tmp_path):
    settings, reached = _bot_channels(monkeypatch, tmp_path)
    _WebClient.error = TimeoutError("timed out")

    delivered = send_human_intervention_alert(
        _keyed_alert(), settings, issued_at=1_000, deadline=time.monotonic() + 20
    )

    assert delivered == ["jira", "email"]
    assert _WebClient.built[0]["timeout"] <= 5


def test_slack_is_skipped_when_under_a_second_is_left_before_the_activity_deadline(
    monkeypatch, tmp_path
):
    settings, _ = _bot_channels(monkeypatch, tmp_path)

    delivered = send_human_intervention_alert(
        _keyed_alert(), settings, issued_at=1_000, deadline=time.monotonic() + 0.5
    )

    assert "slack" not in delivered
    assert _WebClient.built == []


def test_the_slack_sdk_logs_no_request_bodies_when_the_platform_logs_at_debug(
    monkeypatch, tmp_path
):
    settings, _ = _bot_channels(monkeypatch, tmp_path)
    monkeypatch.setattr(logging.getLogger("slack_sdk"), "level", logging.NOTSET)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)
        assert not logging.getLogger("slack_sdk.web.base_client").isEnabledFor(logging.DEBUG)
    finally:
        logging.getLogger().setLevel(logging.WARNING)


def test_an_alert_reads_the_escalation_file_once_for_every_channel(monkeypatch, tmp_path):
    settings, reached = _bot_channels(monkeypatch, tmp_path)
    loads: list[Settings] = []
    load = notifications.load_escalation_config

    def counting_load(settings, **kwargs):
        loads.append(settings)
        return load(settings, **kwargs)

    monkeypatch.setattr(notifications, "load_escalation_config", counting_load)

    delivered = send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)

    assert delivered == ["jira", "email", "slack"]
    assert len(loads) == 1


_SHARED_SLACK_FILE = (
    "config_version: 1\n"
    "slack:\n"
    "  app: shared\n"
    "  transport: webhook\n"
    "  channel: C0123456789\n"
    "  profile: private\n"
)


def _shared_channels(monkeypatch, tmp_path, *, status=200, reply=None, error=None):
    settings, reached = _every_channel(monkeypatch, tmp_path)
    (tenant_dir(tmp_path) / "escalation.yaml").write_text(_SHARED_SLACK_FILE)
    (tmp_path / "slack-shared-app.yaml").write_text(SHARED_APP_YAML)
    token_file = tmp_path / "slack-posting-token"
    token_file.write_text("projected-token")
    _WebClient.built, _WebClient.posts, _WebClient.error = [], [], None
    monkeypatch.setattr("services.integration.slack.client.WebClient", _WebClient)
    posts: list[dict] = []

    def fake_post(url, **kwargs):
        posts.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return httpx.Response(
            status, json=reply or {"ts": "1700000000.000100"}, request=httpx.Request("POST", url)
        )

    class _PostingClient:
        post = staticmethod(fake_post)

    monkeypatch.setattr(
        "services.integration.slack.poster_client._client", lambda ca_file: _PostingClient()
    )
    settings = settings.model_copy(
        update={
            "slack_bot_token": "xoxb-own-app",
            "escalation_action_key": "key",
            "slack_posting_url": "https://slack-poster.platform.svc.cluster.local:8160",
            "slack_posting_token_file": str(token_file),
            "slack_posting_ca_file": str(tmp_path / "slack-poster-ca.pem"),
            "tenant": "adp",
        }
    )
    return settings, reached, posts


def test_a_shared_app_tenant_posts_its_alert_through_the_posting_service(monkeypatch, tmp_path):
    settings, reached, posts = _shared_channels(monkeypatch, tmp_path)

    delivered = send_human_intervention_alert(
        _keyed_alert(), settings, issued_at=1_000, deadline=time.monotonic() + 20
    )

    assert delivered == ["jira", "email", "slack"]
    assert reached == ["jira", "email"]
    assert _WebClient.built == []
    [post] = posts
    assert post["url"] == "https://slack-poster.platform.svc.cluster.local:8160/v1/messages"
    assert set(post["json"]) == {"text", "blocks"}
    actions = [block for block in post["json"]["blocks"] if block["type"] == "actions"]
    assert [button["action_id"] for button in actions[0]["elements"]] == ["approve", "reject"]
    assert post["headers"]["Authorization"] == "Bearer projected-token"
    assert 1 <= float(post["headers"]["X-Alert-Deadline-Seconds"]) <= 5


def test_a_posting_service_refusal_leaves_slack_out_and_names_the_code(
    monkeypatch, tmp_path, caplog
):
    settings, _, posts = _shared_channels(
        monkeypatch, tmp_path, status=502, reply={"error": "not_in_channel"}
    )

    with caplog.at_level(logging.WARNING, logger="services.orchestration.notifications"):
        delivered = send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)

    assert delivered == ["jira", "email"]
    assert len(posts) == 1
    assert "not_in_channel" in caplog.text


def test_a_malformed_shared_app_file_still_delivers_jira_and_email(monkeypatch, tmp_path, caplog):
    settings, reached, posts = _shared_channels(monkeypatch, tmp_path)
    (tmp_path / "slack-shared-app.yaml").write_text("app_id: [unclosed\n")

    with caplog.at_level(logging.ERROR, logger="services.orchestration.notifications"):
        delivered = send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000)

    assert delivered[:2] == ["jira", "email"]
    assert reached[:2] == ["jira", "email"]
    assert posts == []
    assert "escalation.yaml is unusable" in caplog.text


def test_a_shared_app_post_is_skipped_when_under_a_second_is_left_before_the_deadline(
    monkeypatch, tmp_path
):
    settings, _, posts = _shared_channels(monkeypatch, tmp_path)

    delivered = send_human_intervention_alert(
        _keyed_alert(), settings, issued_at=1_000, deadline=time.monotonic() + 0.5
    )

    assert "slack" not in delivered
    assert posts == []
