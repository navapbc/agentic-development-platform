"""Unit tests for the human-intervention alert fan-out."""

from __future__ import annotations

from scripts.config import Settings
from services.orchestration.notifications import (
    HumanInterventionAlert,
    send_human_intervention_alert,
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

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        return _Resp()

    monkeypatch.setattr("services.orchestration.notifications.urllib.request.urlopen", fake_urlopen)
    settings = Settings(
        smtp_host="",
        alert_email_from="",
        alert_email_to="",
        slack_webhook_url="https://hook",
    )

    delivered = send_human_intervention_alert(_alert(), settings)

    assert delivered == ["slack"]
    assert len(calls) == 1


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
