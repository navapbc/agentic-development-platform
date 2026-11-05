"""Human alert fan-out over Jira comment, Slack, and email backup."""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx
from scripts.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertMessage:
    subject: str
    body: str
    issue_key: str


@dataclass(frozen=True)
class HumanInterventionAlert:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    reason: str
    stage: str
    detail: str = ""


def _post_jira_comment(settings: Settings, message: AlertMessage) -> bool:
    if not message.issue_key:
        return False
    from services.integration.tickets import build_ticket_backend_for_issue

    build_ticket_backend_for_issue(message.issue_key, settings).add_comment(
        message.issue_key, message.body
    )
    return True


def _post_slack(settings: Settings, message: AlertMessage) -> bool:
    if not settings.slack_webhook_url:
        return False
    if urlsplit(settings.slack_webhook_url).scheme != "https":
        raise ValueError("SLACK_WEBHOOK_URL must be an https URL")
    httpx.post(
        settings.slack_webhook_url, json={"text": message.body}, timeout=10
    ).raise_for_status()
    return True


def _send_email(settings: Settings, message: AlertMessage) -> bool:
    if not (settings.smtp_host and settings.alert_email_from and settings.alert_email_to):
        return False
    email = EmailMessage()
    email["Subject"] = message.subject
    email["From"] = settings.alert_email_from
    email["To"] = settings.alert_email_to
    email.set_content(message.body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        client.send_message(email)
    return True


def deliver_alert(message: AlertMessage, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    channels: tuple[tuple[str, Callable[[], bool]], ...] = (
        ("jira", lambda: _post_jira_comment(settings, message)),
        ("slack", lambda: _post_slack(settings, message)),
        ("email", lambda: _send_email(settings, message)),
    )
    delivered: list[str] = []
    for channel, sender in channels:
        try:
            if sender():
                delivered.append(channel)
        except Exception:  # noqa: BLE001
            log.warning("alert delivery over %s failed", channel, exc_info=True)
    return delivered


def _intervention_message(alert: HumanInterventionAlert) -> AlertMessage:
    body = (
        f"Human intervention required for {alert.task_id} on {alert.repo}.\n"
        f"The run stopped at the {alert.stage} stage with reason {alert.reason}.\n"
        f"Task {alert.task_id}, execution {alert.execution_id}, workflow {alert.workflow_id}.\n"
        f"{alert.detail}"
    )
    return AlertMessage(
        subject=f"Human intervention needed on {alert.repo} for {alert.task_id}",
        body=body,
        issue_key=alert.issue_key,
    )


def send_human_intervention_alert(
    alert: HumanInterventionAlert, settings: Settings | None = None
) -> list[str]:
    return deliver_alert(_intervention_message(alert), settings)


def send_intake_unroutable_alert(
    *, issue_key: str, project: str, reason: str, settings: Settings | None = None
) -> list[str]:
    body = (
        f"Ticket intake could not resolve a target repository for {issue_key} "
        f"in project {project}.\n"
        f"{reason}.\n"
        "Add a repos marker to the ticket description or set a default_repo for the project, "
        "then intake retries on the next poll."
    )
    message = AlertMessage(
        subject=f"Ticket intake could not route {issue_key}",
        body=body,
        issue_key=issue_key,
    )
    return deliver_alert(message, settings)
