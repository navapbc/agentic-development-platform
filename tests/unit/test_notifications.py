"""Unit tests for the human-intervention alert fan-out."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace

import httpx
import pytest
import yaml
from scripts.config import Settings
from services.operator_remedy import NEXT_STEP_PREFIX
from services.orchestration import model_path_alerts, notifications, remedies
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


def test_alert_detail_masks_credentials_and_keeps_the_text_around_them():
    from services.orchestration.notifications import bounded_detail

    detail = (
        "remote https://x-access-token:s3cr3t-value@github.com/o/r.git\n"
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        "key AKIAIOSFODNN7EXAMPLE and Authorization: Bearer abc.def\n"
        "the token flow keeps Priority.Normal"
    )

    masked = bounded_detail(detail)

    assert masked == (
        "remote https://[masked]@github.com/o/r.git\n"
        "AWS_SECRET_ACCESS_KEY=[masked]\n"
        "key [masked] and Authorization: Bearer [masked]\n"
        "the token flow keeps Priority.Normal"
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


def test_the_model_error_summary_keeps_only_the_provider_message():
    from services.orchestration.notifications import model_error_summary

    detail = (
        'litellm returned 500 with {"error":{"message":"litellm.APIConnectionError: '
        "BedrockException - Cannot connect to host bedrock-runtime.us-east-1.amazonaws.com:443 "
        "ssl:<ssl.SSLContext object at 0xffff7bc47c70> [None]\\n\\nLiteLLM: model group "
        '\'planning-model\' failed with the error above.","type":"internal_server_error"}}'
    )

    assert model_error_summary(detail) == (
        "BedrockException - Cannot connect to host bedrock-runtime.us-east-1.amazonaws.com:443"
    )


@pytest.mark.parametrize("posture", ["host", "deployed"])
def test_a_tenant_s_slack_post_names_its_buttons_and_no_command_in_either_posture(
    monkeypatch, tmp_path, posture
):
    settings, _ = _bot_channels(monkeypatch, tmp_path)
    settings = settings.model_copy(update={"deployment_posture": posture, "tenant": "adp"})
    bodies: list[str] = []
    monkeypatch.setattr(
        notifications.JiraCommentChannel,
        "deliver",
        lambda self, message, *args: bodies.append(message.body) or True,
    )
    structured = remedies.run_failed_remedy("T-1", "review")
    alert = replace(
        _alert(),
        reason="run_failed",
        structured_remedy=structured,
        remedy=render_remedy(structured, settings),
    )

    send_human_intervention_alert(alert, settings, issued_at=1_000)

    [post] = _WebClient.posts
    text = json.dumps(post["blocks"])
    assert "Restart run button" in _next_step(post)
    assert "inv " not in text and "kubectl" not in text
    assert _action_ids(post) == ["acknowledge", "restart"]
    assert "Slack: This alert is posted in Slack channel C0123456789" in bodies[0]
    assert "inv " not in bodies[0] and "kubectl" not in bodies[0]


TERMINAL_ALERTS = [
    ("plan_rejected", remedies.plan_rejected_remedy("T-1")),
    ("gate_rejected", remedies.gate_rejected_remedy("T-1", "plan")),
    ("review_blocked", remedies.review_blocked_remedy("T-1", "security-review")),
    ("policy_denied", remedies.policy_denied_remedy("T-1", "no_secrets")),
    ("existing_pr", remedies.existing_pr_remedy("T-1", "https://github.test/pr/1")),
    ("orphan_reclaimed", remedies.orphan_reclaimed_remedy("T-1", "wf-1")),
]


def _slack_post(monkeypatch, tmp_path, alert: HumanInterventionAlert) -> dict:
    settings, _ = _bot_channels(monkeypatch, tmp_path)
    settings = settings.model_copy(update={"tenant": "adp"})
    alert = replace(alert, remedy=render_remedy(alert.structured_remedy, settings))
    send_human_intervention_alert(alert, settings, issued_at=1_000)
    [post] = _WebClient.posts
    return post


def _action_ids(post: dict) -> list[str]:
    actions = [block for block in post["blocks"] if block["type"] == "actions"]
    return [button["action_id"] for block in actions for button in block["elements"]]


def _next_step(post: dict) -> str:
    [next_step] = [
        block["text"]["text"]
        for block in post["blocks"]
        if block.get("text", {}).get("text", "").startswith("*Next step:*")
    ]
    return next_step


@pytest.mark.parametrize(("reason", "structured"), TERMINAL_ALERTS)
def test_a_terminal_tenant_alert_whose_remedy_restarts_the_run_carries_restart_run(
    monkeypatch, tmp_path, reason, structured
):
    post = _slack_post(
        monkeypatch, tmp_path, replace(_alert(), reason=reason, structured_remedy=structured)
    )

    assert _action_ids(post) == ["acknowledge", "restart"]
    next_step = _next_step(post)
    assert "Restart run button" in next_step
    assert "inv " not in next_step and "kubectl" not in next_step


@pytest.mark.parametrize(
    ("reason", "stage", "structured", "decisions"),
    [
        (
            "gate_paused:plan",
            "plan",
            remedies.gate_decision_remedy("T-1", "plan"),
            ["approve", "reject"],
        ),
        (
            "repair_exhausted",
            "repair",
            remedies.repair_remedy("repair_exhausted", issue_key="T-1", execution_id="exec-1"),
            ["retry", "abandon"],
        ),
        (
            "impl_failed",
            "implementation",
            remedies.implementation_remedy("impl_failed", issue_key="T-1", execution_id="exec-1"),
            [],
        ),
    ],
)
def test_a_park_that_waits_for_a_decision_carries_its_decisions_and_no_restart_run(
    monkeypatch, tmp_path, reason, stage, structured, decisions
):
    parked = replace(
        _alert(),
        reason=reason,
        stage=stage,
        structured_remedy=structured,
        park_key=f"exec-1:{stage}:1",
    )

    assert _action_ids(_slack_post(monkeypatch, tmp_path, parked)) == decisions


ADMIN_EMAILS = ("platform-admins@example.com",)


def _admin_settings(**overrides) -> Settings:
    return hermetic_settings(
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="mail.local",
        alert_email_from="agent@example.com",
        alert_email_to="oncall@example.com",
        slack_posting_url="https://slack-poster.adp.svc:8160",
        tenant="acme",
        **overrides,
    )


def _capture_admin(monkeypatch) -> tuple[list[str], list[dict]]:
    emailed: list[str] = []
    posted: list[dict] = []

    class _SMTP:
        def __init__(self, host, port, timeout=0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send_message(self, message):
            emailed.append(f"{message['To']}|{message.get_content()}")

    def fake_post_message(settings, **kwargs):
        from services.integration.slack.poster_client import PostedMessage

        posted.append(kwargs)
        return PostedMessage("1700000000.000200", "owner-of-1700000000")

    monkeypatch.setattr("services.orchestration.notifications.smtplib.SMTP", _SMTP)
    monkeypatch.setattr("services.integration.slack.poster_client.post_message", fake_post_message)
    return emailed, posted


@pytest.mark.parametrize(
    ("administrators", "expected"),
    [
        (
            {"slack": {"channel": "C0ADMIN"}, "email": list(ADMIN_EMAILS)},
            ["admin_email", "admin_slack"],
        ),
        ({"slack": {"channel": "C0ADMIN"}}, ["admin_slack"]),
        ({"email": list(ADMIN_EMAILS)}, ["admin_email"]),
    ],
)
def test_the_admin_alert_reaches_the_destinations_platform_escalation_names(
    monkeypatch, administrators, expected
):
    from services.integration.slack.shared_app import SharedSlackApp
    from services.orchestration.platform_escalation import parse_platform_escalation

    shared_app = SharedSlackApp("A0SHARED001", "E0SHARED001", "https://slack.com/api/")
    destination = parse_platform_escalation(
        {"config_version": 1, "administrators": administrators}, shared_app
    )
    emailed, posted = _capture_admin(monkeypatch)
    message = notifications.AlertMessage(subject="Platform fault", body="fix", issue_key="")

    delivered = notifications.deliver_admin_alert(message, _admin_settings(), destination)

    assert delivered == expected
    assert [entry.split("|")[0] for entry in emailed] == (
        [", ".join(ADMIN_EMAILS)] if "admin_email" in expected else []
    )
    assert [post["admin"] for post in posted] == ([True] if "admin_slack" in expected else [])
    assert [(post.admin, post.ts) for post in message.posts] == (
        [(True, "1700000000.000200")] if "admin_slack" in expected else []
    )


@pytest.mark.parametrize(
    ("administrators", "refused"),
    [
        ({"slack": {"channel": "C0ADMIN"}}, True),
        ({"slack": {"channel": "C0ADMIN"}, "email": list(ADMIN_EMAILS)}, True),
        ({"email": list(ADMIN_EMAILS)}, False),
    ],
    ids=["channel", "channel-and-email", "email"],
)
def test_an_administrator_channel_without_the_shared_app_is_refused_at_preflight(
    monkeypatch, tmp_path, administrators, refused
):
    from services.integration.credentials import ConfigError
    from services.orchestration import platform_escalation

    (tmp_path / platform_escalation.PLATFORM_ESCALATION_FILE).write_text(
        yaml.safe_dump({"config_version": 1, "administrators": administrators})
    )
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(platform_escalation, "process_shared_app", lambda: None)

    if refused:
        with pytest.raises(ConfigError, match="no slack-shared-app.yaml"):
            platform_escalation.platform_escalation_preflight()
    else:
        assert platform_escalation.platform_escalation_preflight().emails == ADMIN_EMAILS


def test_a_platform_escalation_that_names_no_destination_is_refused():
    from services.integration.credentials import ConfigError
    from services.orchestration.platform_escalation import parse_platform_escalation

    with pytest.raises(ConfigError, match="names neither slack.channel nor email"):
        parse_platform_escalation({"config_version": 1, "administrators": {}}, None)


def _platform_fault_alert(reason: str, detail: str) -> notifications.HumanInterventionAlert:
    return notifications.HumanInterventionAlert(
        task_id="ACME-7",
        execution_id="exec-7",
        workflow_id="draft-pr-ACME-7",
        repo="acme/app",
        issue_key="ACME-7",
        reason=reason,
        stage="implement",
        detail=detail,
        remedy="Run kubectl -n adp-platform rollout restart deployment/litellm",
        structured_remedy=remedies.model_path_unavailable_remedy("bedrock_path"),
    )


INTERNALS = (
    "https://litellm.adp-platform.svc:4000",
    "arn:aws:iam::123456789012:role/litellm",
    "tenant beta",
)


def test_a_platform_fault_tells_the_tenant_the_effect_and_the_admins_the_fix(monkeypatch):
    from services.orchestration.platform_escalation import AdminDestination

    emailed, _ = _capture_admin(monkeypatch)
    posted = _record_slack_posts(monkeypatch)
    monkeypatch.setattr(
        "services.orchestration.platform_escalation.process_admin_destination",
        lambda: AdminDestination(emails=ADMIN_EMAILS),
    )
    detail = f"ModelUnavailable: {' '.join(INTERNALS)}"

    send_human_intervention_alert(
        _platform_fault_alert("model_unavailable", detail), _admin_settings()
    )

    tenant_text = json.dumps(posted) + "".join(
        entry for entry in emailed if entry.startswith("oncall@")
    )
    admin_text = "".join(entry for entry in emailed if entry.startswith("platform-admins@"))
    assert notifications.PLATFORM_RESUMES in tenant_text
    assert not [internal for internal in INTERNALS if internal in tenant_text]
    assert "kubectl" not in tenant_text and "inv " not in tenant_text
    assert all(internal in admin_text for internal in INTERNALS)
    assert NEXT_STEP_PREFIX in admin_text


def test_a_run_failed_on_a_platform_error_sends_its_cause_to_the_admins(monkeypatch):
    from services.orchestration.platform_escalation import AdminDestination

    emailed, _ = _capture_admin(monkeypatch)
    _record_slack_posts(monkeypatch)
    monkeypatch.setattr(
        "services.orchestration.platform_escalation.process_admin_destination",
        lambda: AdminDestination(emails=ADMIN_EMAILS),
    )

    send_human_intervention_alert(
        _platform_fault_alert("run_failed", "OperationalError: the database refused"),
        _admin_settings(),
    )
    send_human_intervention_alert(
        _platform_fault_alert("run_failed", "AssertionError: a test failed"), _admin_settings()
    )

    admin = [entry for entry in emailed if entry.startswith("platform-admins@")]
    assert len(admin) == 1
    assert "OperationalError: the database refused" in admin[0]


SLACK_RESOLVER = "slack:E097S1KLTQU/U09UFK0BUQP"


def _recovery(monkeypatch, resolver: str, outage_post) -> tuple[list, list]:
    messages, edits = [], []
    for name in ("deliver_alert", "deliver_admin_alert"):
        monkeypatch.setattr(
            notifications, name, lambda message, *args: messages.append(message) or []
        )
    monkeypatch.setattr(
        notifications, "update_slack_post", lambda post, **kwargs: edits.append(kwargs) or True
    )
    model_path_alerts.send_model_path_recovered_alert(
        hop="credentials",
        issue_key="T-1",
        remedy="",
        settings=hermetic_settings(tenant="acme"),
        outage_post=outage_post,
        resolver=resolver,
    )
    return messages, edits


def test_a_slack_resolver_is_a_mention_in_slack_and_a_user_id_in_email(monkeypatch):
    messages, [edit] = _recovery(
        monkeypatch, SLACK_RESOLVER, notifications.SlackPostRef("C1", "1.1")
    )

    line = edit["blocks"][-1]["elements"][0]["text"]
    assert line.startswith("Resolved by <@U09UFK0BUQP> at ")
    assert line.endswith(". Fix done pressed.")
    assert "slack:" not in line and "&lt;@" not in line
    [admin] = [message for message in messages if "Resolution:" in message.body]
    assert "Resolution: Fix done pressed by Slack user U09UFK0BUQP." in admin.body
    assert "slack:" not in admin.body


def test_a_post_recorded_without_blocks_resolves_to_its_title_and_failing_hop(monkeypatch):
    messages, [edit] = _recovery(monkeypatch, "", notifications.SlackPostRef("C1", "1.1"))

    texts = [json.dumps(block) for block in edit["blocks"]]
    assert len(texts) == 3
    assert model_path_alerts.model_path_title(hermetic_settings(tenant="acme")) in texts[0]
    assert "Failing hop" in texts[1] and "AWS credentials" in texts[1]
    assert edit["blocks"][-1]["elements"][0]["text"].endswith("Intake found the path healthy.")
    [admin] = [message for message in messages if "Resolution:" in message.body]
    assert "Resolution: Intake found the path healthy." in admin.body


def _numbered_posting(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        ts = kwargs["json"].get("ts") or f"1700000000.{len(calls):06d}"
        return httpx.Response(200, json={"ts": ts}, request=httpx.Request("POST", url))

    class _PostingClient:
        post = staticmethod(fake_post)

    monkeypatch.setattr(
        "services.integration.slack.poster_client._client", lambda ca_file: _PostingClient()
    )
    return calls


def _activity_environment():
    import uuid
    from datetime import UTC, datetime, timedelta

    from temporalio.testing import ActivityEnvironment

    environment = ActivityEnvironment()
    now = datetime.now(UTC)
    environment.info = replace(
        environment.info,
        activity_id=uuid.uuid4().hex,
        scheduled_time=now,
        started_time=now,
        start_to_close_timeout=timedelta(seconds=30),
    )
    return environment


def _has_actions(blocks) -> bool:
    return any(block.get("type") == "actions" for block in blocks)


def _button_values(blocks) -> list[str]:
    return [
        button["value"]
        for block in blocks
        if block.get("type") == "actions"
        for button in block["elements"]
    ]


def test_fresh_park_buttons_verify_in_the_thread_and_the_alert_buttons_stay_expired(
    monkeypatch, tmp_path
):
    from services.integration.slack.tokens import TokenRefused, verify_action_token

    settings, _, _ = _shared_channels(monkeypatch, tmp_path)
    calls = _numbered_posting(monkeypatch)
    collected: list[notifications.SlackPostRef] = []
    send_human_intervention_alert(_keyed_alert(), settings, issued_at=1_000, posts=collected)
    [original] = collected
    refreshed_at = int(time.time())

    fresh = notifications.post_park_buttons(
        _keyed_alert(), settings, thread=original, issued_at=refreshed_at, reminder=True
    )

    assert fresh is not None and fresh.ts != original.ts
    assert calls[-1]["json"]["thread_ts"] == original.ts
    [old_value] = _button_values(json.loads(original.blocks))[:1]
    with pytest.raises(TokenRefused) as refused:
        verify_action_token(old_value, b"key")
    assert refused.value.reason == "expired"
    assert refused.value.token is not None
    assert refused.value.token.park_key == _keyed_alert().park_key
    fresh_values = _button_values(json.loads(fresh.blocks))
    assert fresh_values
    for value in fresh_values:
        token = verify_action_token(value, b"key")
        assert token.park_key == _keyed_alert().park_key
        assert token.expires_at == refreshed_at + 72 * 3600


async def test_refreshes_and_a_decision_leave_one_post_of_the_park_with_buttons(
    monkeypatch, tmp_path
):
    import scripts.config
    from services.coordination.idempotency import InMemorySideEffectLedger
    from services.orchestration.temporal_activities import notify

    settings, _, _ = _shared_channels(monkeypatch, tmp_path)
    calls = _numbered_posting(monkeypatch)
    monkeypatch.setattr(scripts.config, "get_settings", lambda: settings)
    ledger = InMemorySideEffectLedger()
    monkeypatch.setattr(notify, "_alert_ledger", lambda: ledger)
    keyed = _keyed_alert()
    request = notify.AlertHumanInterventionRequest(
        task_id=keyed.task_id,
        execution_id=keyed.execution_id,
        workflow_id=keyed.workflow_id,
        repo=keyed.repo,
        issue_key=keyed.issue_key,
        reason=keyed.reason,
        stage=keyed.stage,
        remedy=keyed.structured_remedy,
        park_key=keyed.park_key,
    )

    def latest_blocks() -> dict[str, list]:
        latest: dict[str, list] = {}
        for index, call in enumerate(calls):
            body = call["json"]
            latest[body.get("ts") or f"1700000000.{index + 1:06d}"] = body.get("blocks") or []
        return latest

    alerted = await _activity_environment().run(notify.alert_human_intervention, request)
    original = alerted.park_post
    assert original is not None and _has_actions(json.loads(original.blocks))
    assert alerted.ttl_hours == 72 and alerted.issued_at

    first = await _activity_environment().run(
        notify.refresh_park_buttons,
        notify.RefreshParkButtonsRequest(alert=request, posts=[original], reminder=True),
    )

    moved_original, first_thread = first.posts
    edited = json.loads(moved_original.blocks)
    assert not _has_actions(edited)
    original_content = [b for b in json.loads(original.blocks) if b.get("type") != "actions"]
    assert edited[: len(original_content)] == original_content
    assert edited[-1]["elements"][0]["text"] == (
        "These buttons expired. Fresh buttons are posted in this message's thread."
    )
    assert _has_actions(json.loads(first_thread.blocks))

    second = await _activity_environment().run(
        notify.refresh_park_buttons,
        notify.RefreshParkButtonsRequest(alert=request, posts=first.posts),
    )

    assert [post.ts for post in second.posts[:2]] == [original.ts, first_thread.ts]
    superseded = json.loads(second.posts[1].blocks)
    assert not _has_actions(superseded)
    assert "Newer buttons follow in this thread" in superseded[-1]["elements"][0]["text"]
    assert _has_actions(json.loads(second.posts[2].blocks))

    await _activity_environment().run(
        notify.settle_park_posts,
        notify.SettleParkPostsRequest(
            posts=second.posts, decision="approved", actor="slack:E-ORG/U-LEAD"
        ),
    )

    latest = latest_blocks()
    assert set(latest) == {post.ts for post in second.posts}
    assert not any(_has_actions(blocks) for blocks in latest.values())
    assert "Approved by <@U-LEAD>" in latest[original.ts][-1]["elements"][0]["text"]


def _limit_park_alert(execution_id: str = "exec-1", thread_ts: str = "") -> HumanInterventionAlert:
    park_key = f"{execution_id}:repair:IMPLEMENTATION_MAX_COST_USD:1"
    return replace(
        _alert(),
        execution_id=execution_id,
        reason="cost_budget_exceeded",
        stage="repair",
        structured_remedy=remedies.repair_remedy(
            "cost_budget_exceeded", issue_key="T-1", execution_id=execution_id
        ),
        park_key=park_key,
        limit_request=f"0.4:{park_key}",
        limit_amounts=(0.4,),
        thread_ts=thread_ts,
    )


def _action_elements(post: dict) -> list[dict]:
    return [
        element
        for block in post["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
    ]


def test_a_run_limit_park_alert_carries_park_all_jobs_signed_for_the_tenant_jobs(
    monkeypatch, tmp_path
):
    from services.integration.slack.tokens import verify_action_token

    settings, _ = _bot_channels(monkeypatch, tmp_path)

    send_human_intervention_alert(_limit_park_alert(), settings, issued_at=1_000)

    [post] = _WebClient.posts
    elements = {element["action_id"]: element for element in _action_elements(post)}
    assert {"request_limit", "acknowledge", "abandon", "pause_jobs"} <= set(elements)
    token = verify_action_token(elements["pause_jobs"]["value"], b"key", now=1_000)
    assert (token.alert, token.actions) == ("tenant_jobs:run:exec-1", ("pause_jobs",))


async def test_two_runs_parked_on_a_spent_period_reply_in_the_one_period_alert_thread(
    monkeypatch, tmp_path
):
    from datetime import UTC, datetime

    from services.coordination.idempotency import InMemorySideEffectLedger
    from services.orchestration.limit_grants import BudgetPeriod
    from services.orchestration.tenant_jobs import PeriodBudget, ensure_period_alert

    settings, _ = _bot_channels(monkeypatch, tmp_path)
    ledger = InMemorySideEffectLedger()
    period = BudgetPeriod(
        start=datetime(2026, 11, 1, tzinfo=UTC), reset=datetime(2026, 12, 1, tzinfo=UTC)
    )
    budget = PeriodBudget(configured=100.0, effective=100.0, spend=100.0, period=period)
    top_level: list[PeriodBudget] = []

    async def send(spent: PeriodBudget) -> dict:
        top_level.append(spent)
        return {"channels": ["slack"], "channel": "C0123456789", "ts": "100.1"}

    for execution_id in ("exec-1", "exec-2"):
        thread = await ensure_period_alert(ledger, "acme", budget, send)
        alert = replace(
            _limit_park_alert(execution_id, thread.ts),
            reason="tenant_model_budget_exceeded",
            limit_request="",
            limit_amounts=(),
        )
        send_human_intervention_alert(alert, settings, issued_at=1_000)

    assert len(top_level) == 1
    assert [post.get("thread_ts") for post in _WebClient.posts] == ["100.1", "100.1"]
    assert all(
        [element["action_id"] for element in _action_elements(post)] == ["abandon"]
        for post in _WebClient.posts
    )


async def test_a_period_budget_spent_again_after_a_grant_sends_a_new_alert_the_park_replies_in(
    monkeypatch, tmp_path
):
    from datetime import UTC, datetime

    from services.coordination.idempotency import InMemorySideEffectLedger
    from services.orchestration.limit_grants import (
        GRANTED,
        PERIOD_SCOPE,
        BudgetPeriod,
        LimitGrant,
    )
    from services.orchestration.tenant_jobs import (
        PeriodBudget,
        ensure_period_alert,
        period_budget_message,
    )

    settings, _ = _bot_channels(monkeypatch, tmp_path)
    settings = settings.model_copy(update={"tenant_model_budget_usd": 100.0})
    period = BudgetPeriod(
        start=datetime(2026, 11, 1, tzinfo=UTC), reset=datetime(2026, 12, 1, tzinfo=UTC)
    )
    granted: list[LimitGrant] = []

    class _Grants:
        async def for_limit(self, tenant: str, limit_name: str) -> list[LimitGrant]:
            return list(granted)

    top_level: list[list[dict]] = []

    async def send(spent: PeriodBudget) -> dict:
        message = await period_budget_message(settings, spent, grants=_Grants())
        top_level.append(message.slack_blocks)
        return {"channels": ["slack"], "channel": "C0123456789", "ts": f"100.{len(top_level)}"}

    ledger = InMemorySideEffectLedger()
    first = PeriodBudget(configured=100.0, effective=100.0, spend=100.0, period=period)
    await ensure_period_alert(ledger, "acme", first, send)
    await ensure_period_alert(ledger, "acme", first, send)
    granted.append(
        LimitGrant(
            request_id="req-1",
            tenant="acme",
            limit_name="TENANT_MODEL_BUDGET_USD",
            scope=PERIOD_SCOPE,
            amount=10.0,
            state=GRANTED,
            requester="slack:T1/UTENANT",
            period_start=period.start,
            expires_at=period.reset,
        )
    )
    spent_again = PeriodBudget(configured=100.0, effective=110.0, spend=110.0, period=period)
    thread = await ensure_period_alert(ledger, "acme", spent_again, send)
    alert = replace(
        _limit_park_alert("exec-3", thread.ts),
        reason="tenant_model_budget_exceeded",
        limit_request="",
        limit_amounts=(),
    )
    send_human_intervention_alert(alert, settings, issued_at=1_000)

    assert len(top_level) == 2
    second = [
        element
        for block in top_level[1]
        if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert [element["action_id"] for element in second] == [
        "request_limit",
        "acknowledge",
        "pause_jobs",
    ]
    assert [option["value"] for option in second[0]["options"]] == ["10", "25"]
    [reply] = _WebClient.posts
    assert reply["thread_ts"] == thread.ts == "100.2"


_NPM_CI_CAUSE = (
    "npm error The `npm ci` command can only install with an existing package-lock.json or"
)
_NPM_CI_OUTPUT = "\n".join(
    [
        "npm error code EUSAGE",
        _NPM_CI_CAUSE,
        *(f"npm error   --usage-option-{n}  usage text for option {n}" for n in range(3, 120)),
        "npm error A complete log of this run can be found in: /root/.npm/_logs/debug-0.log",
    ]
)


def test_a_long_npm_ci_detail_keeps_its_cause_and_its_tail_with_one_omission_note():
    alert = replace(_alert(), stage="checks", reason="checks_failed", detail=_NPM_CI_OUTPUT)

    body = notifications._intervention_message(alert).body

    assert _NPM_CI_CAUSE in body
    assert "A complete log of this run can be found in" in body
    [note] = re.findall(r"\[(\d+) lines and (\d+) characters omitted\]", body)
    body_lines = set(body.splitlines())
    omitted = [line for line in _NPM_CI_OUTPUT.splitlines() if line not in body_lines]
    assert note == (str(len(omitted)), str(sum(len(line) + 1 for line in omitted)))
    assert len(omitted) == 120 - notifications.DETAIL_LINE_LIMIT
