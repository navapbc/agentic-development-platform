"""Unit tests for the human-intervention alert fan-out."""

from __future__ import annotations

import logging
from dataclasses import replace

import httpx
import pytest
from scripts.config import Settings
from services.operator_remedy import NEXT_STEP_PREFIX, NO_NEXT_STEP
from services.orchestration.notifications import (
    HumanInterventionAlert,
    send_human_intervention_alert,
    send_intake_unroutable_alert,
)


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
    settings = Settings(
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
    settings = Settings(
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
    settings = Settings(
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
    settings = Settings(
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
    settings = Settings(
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
    settings = Settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    assert send_human_intervention_alert(_alert(), settings) == []


def _unconfigured() -> Settings:
    return Settings(
        slack_webhook_url="",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )


def test_the_remedy_renders_as_a_next_step_line(monkeypatch):
    posted: list[dict[str, object]] = []

    def fake_post(url, *, json, timeout):
        posted.append(json)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )
    alert = replace(_alert(), remedy="Run `inv repair --issue ADSER-24 --retry`.")

    send_human_intervention_alert(alert, settings)

    assert posted[0]["text"].endswith(
        f"{NEXT_STEP_PREFIX} Run `inv repair --issue ADSER-24 --retry`."
    )


@pytest.mark.parametrize("remedy", ["", NO_NEXT_STEP])
def test_an_alert_with_no_next_step_renders_no_next_step_line(monkeypatch, remedy):
    posted: list[dict[str, object]] = []

    def fake_post(url, *, json, timeout):
        posted.append(json)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    send_human_intervention_alert(replace(_alert(), remedy=remedy), settings)

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


def test_the_unroutable_intake_alert_renders_its_required_remedy(monkeypatch):
    posted: list[dict[str, object]] = []

    def fake_post(url, *, json, timeout):
        posted.append(json)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        slack_webhook_url="https://hooks.slack.test/x",
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
    )

    send_intake_unroutable_alert(
        issue_key="ADSER-24",
        project="ADSER",
        reason="no allowlisted repository",
        remedy="Add the repository to github.yaml.",
        settings=settings,
    )

    assert f"{NEXT_STEP_PREFIX} Add the repository to github.yaml." in posted[0]["text"]
