"""Human-intervention alert fan-out for the repair loop: Jira comment, Slack, email backup."""

from __future__ import annotations

import json
import smtplib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from scripts.config import Settings, get_settings


@dataclass(frozen=True)
class HumanInterventionAlert:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    attempts: int
    ci_build_id: str
    failure_summary: str


def _alert_body(alert: HumanInterventionAlert) -> str:
    return (
        f"Automated repair exhausted after {alert.attempts} attempts on {alert.repo}.\n"
        f"Task {alert.task_id}, execution {alert.execution_id}, workflow {alert.workflow_id}, "
        f"CI build {alert.ci_build_id}.\n"
        "Human intervention needed. Resume the workflow with a retry or abandon decision.\n\n"
        f"{alert.failure_summary}"
    )


def _post_jira_comment(settings: Settings, alert: HumanInterventionAlert, body: str) -> bool:
    if not (settings.jira_url and alert.issue_key):
        return False
    from services.integration.jira.adapter import build_jira_adapter

    build_jira_adapter(settings).add_comment(alert.issue_key, body)
    return True


def _post_slack(settings: Settings, body: str) -> bool:
    if not settings.slack_webhook_url:
        return False
    payload = json.dumps({"text": body}).encode("utf-8")
    request = urllib.request.Request(
        settings.slack_webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        return True


def _send_email(settings: Settings, alert: HumanInterventionAlert, body: str) -> bool:
    if not (settings.smtp_host and settings.alert_email_from and settings.alert_email_to):
        return False
    message = EmailMessage()
    message["Subject"] = f"Repair exhausted on {alert.repo} for {alert.task_id}"
    message["From"] = settings.alert_email_from
    message["To"] = settings.alert_email_to
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        client.send_message(message)
    return True


def send_human_intervention_alert(
    alert: HumanInterventionAlert, settings: Settings | None = None
) -> list[str]:
    settings = settings or get_settings()
    body = _alert_body(alert)
    channels: tuple[tuple[str, Callable[[], bool]], ...] = (
        ("jira", lambda: _post_jira_comment(settings, alert, body)),
        ("slack", lambda: _post_slack(settings, body)),
        ("email", lambda: _send_email(settings, alert, body)),
    )
    delivered: list[str] = []
    for channel, sender in channels:
        try:
            if sender():
                delivered.append(channel)
        except Exception:  # noqa: BLE001
            continue
    return delivered
