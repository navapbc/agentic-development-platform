"""Human alert fan-out over Jira comment, Slack, and email backup."""

from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import httpx
from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError
from services.operator_remedy import DeploymentPosture, Remedy, render, render_step, with_remedy
from services.orchestration.escalation import (
    EMAIL,
    JIRA,
    SHARED_APP,
    SLACK,
    EscalationChannel,
    EscalationConfig,
    SlackSettings,
    load_escalation_config,
)

log = logging.getLogger(__name__)


SLACK_TIMEOUT_SECONDS = 5
SLACK_MIN_SECONDS = 1.0


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
    remedy: str = ""
    structured_remedy: Remedy = field(default_factory=Remedy)
    park_key: str = ""


@dataclass(frozen=True)
class AlertMessage:
    subject: str
    body: str
    issue_key: str
    alert: HumanInterventionAlert | None = None
    issued_at: int = 0
    """When the park's alert was first scheduled, so a retried alert signs the same buttons."""
    deadline: float | None = None
    """``time.monotonic()`` value after which the alert activity times out."""


class JiraCommentChannel:
    name = JIRA

    def deliver(self, message: AlertMessage, settings: Settings, config: EscalationConfig) -> bool:
        if not message.issue_key:
            return False
        from services.integration.tickets import build_ticket_backend_for_issue

        build_ticket_backend_for_issue(message.issue_key, settings).add_comment(
            message.issue_key, message.body
        )
        return True


class EmailChannel:
    name = EMAIL

    def deliver(self, message: AlertMessage, settings: Settings, config: EscalationConfig) -> bool:
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


class SlackChannel:
    name = SLACK

    def deliver(self, message: AlertMessage, settings: Settings, config: EscalationConfig) -> bool:
        slack = config.slack
        if slack is not None and slack.app == SHARED_APP:
            return self._post_through_posting_service(message, settings, slack)
        if settings.slack_bot_token and slack is not None:
            return self._post_with_bot(message, settings, slack)
        if not settings.slack_webhook_url:
            return False
        if urlsplit(settings.slack_webhook_url).scheme != "https":
            raise ValueError("SLACK_WEBHOOK_URL must be an https URL")
        httpx.post(
            settings.slack_webhook_url, json={"text": message.body}, timeout=10
        ).raise_for_status()
        return True

    def _post_with_bot(
        self, message: AlertMessage, settings: Settings, slack: SlackSettings
    ) -> bool:
        from services.integration.slack.client import tenant_web_client

        timeout = _slack_timeout(message)
        if timeout is None:
            return False
        text, blocks = _slack_message(message, settings, slack)
        client = tenant_web_client(
            settings.slack_bot_token, slack.api_base_url, timeout=int(timeout), retry_handlers=[]
        )
        client.chat_postMessage(
            channel=slack.channel,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
        return True

    def _post_through_posting_service(
        self, message: AlertMessage, settings: Settings, slack: SlackSettings
    ) -> bool:
        from services.integration.slack.poster_client import PostingFailed, post_message

        timeout = _slack_timeout(message)
        if timeout is None:
            return False
        text, blocks = _slack_message(message, settings, slack)
        try:
            post_message(settings, text=text, blocks=blocks, timeout=timeout)
        except PostingFailed as exc:
            log.warning("the alert was not posted to Slack, because %s", exc)
            return False
        return True


def _slack_timeout(message: AlertMessage) -> float | None:
    """Return the seconds a Slack post may take, or None when the alert deadline leaves too few."""
    timeout = float(SLACK_TIMEOUT_SECONDS)
    if message.deadline is not None:
        timeout = min(timeout, message.deadline - time.monotonic() - SLACK_MIN_SECONDS)
    if timeout < SLACK_MIN_SECONDS:
        log.warning("too little of the alert deadline is left to post to Slack")
        return None
    return timeout


def _slack_message(
    message: AlertMessage, settings: Settings, slack: SlackSettings
) -> tuple[str, list[dict[str, Any]] | None]:
    """Return the alert's text and blocks, with signed decision buttons when the alert parks."""
    from services.integration.slack.messages import ParkContext, SlackAlert, alert_payload

    alert = message.alert
    if alert is None:
        text, blocks = message.body, None
    else:
        park = None
        if alert.park_key and settings.escalation_action_key:
            park = ParkContext(
                tenant=settings.tenant.strip(),
                execution_id=alert.execution_id,
                workflow_id=alert.workflow_id,
                park_key=alert.park_key,
                issued_at=message.issued_at or int(time.time()),
                ttl_hours=slack.token_ttl_hours,
                action_key=settings.escalation_action_key.encode(),
            )
        text, blocks = alert_payload(
            SlackAlert(
                task_id=alert.task_id,
                repo=alert.repo,
                issue_key=alert.issue_key,
                stage=alert.stage,
                reason=alert.reason,
                detail=_bounded_detail(alert.detail),
                remedy_text=alert.remedy,
                remedy=alert.structured_remedy.for_tenant(settings.tenant.strip()),
                remedy_commands=render_remedy_commands(alert.structured_remedy, settings),
                ticket_url=_ticket_url(alert.issue_key, settings),
            ),
            content=slack.content,
            park=park,
        )
    return text, blocks


# Slack runs last so a slow Slack call cannot delay the Jira comment or the email.
CHANNELS: tuple[EscalationChannel, ...] = (JiraCommentChannel(), EmailChannel(), SlackChannel())


def _ticket_url(issue_key: str, settings: Settings) -> str:
    from services.integration.tickets.factory import ticket_url

    if not issue_key:
        return ""
    try:
        return ticket_url(issue_key, settings)
    except ConfigError:
        log.warning("the ticket link of %s could not be built", issue_key, exc_info=True)
        return ""


def _escalation_config(settings: Settings) -> EscalationConfig:
    """Return the tenant's escalation config, or the default that enables every channel."""
    from services.integration.slack.shared_app import process_shared_app

    try:
        return load_escalation_config(settings, shared_app=process_shared_app())
    except ConfigError:
        log.error(
            "escalation.yaml is unusable, so the alert goes to every configured channel",
            exc_info=True,
        )
        return EscalationConfig()


def deliver_alert(message: AlertMessage, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    config = _escalation_config(settings)
    delivered: list[str] = []
    for channel in CHANNELS:
        if channel.name not in config.channels:
            continue
        try:
            if channel.deliver(message, settings, config):
                delivered.append(channel.name)
        except Exception:  # noqa: BLE001
            log.warning("alert delivery over %s failed", channel.name, exc_info=True)
    if not delivered:
        log.error(
            "alert for %s reached no channel. Configure a Jira issue key, SLACK_WEBHOOK_URL, "
            "or SMTP_HOST with ALERT_EMAIL_FROM and ALERT_EMAIL_TO, then re-run the alert.",
            message.issue_key or "an unidentified ticket",
        )
    return delivered


def render_remedy(remedy: Remedy, settings: Settings) -> str:
    """Render a remedy for the posture of the operators who read this deployment's alerts.

    The worker that delivers an alert serves the run's tenant, so its TENANT is bound into every
    action that names none.
    """
    return render(
        remedy.for_tenant(settings.tenant.strip()),
        DeploymentPosture(settings.deployment_posture),
    )


def render_remedy_commands(remedy: Remedy, settings: Settings) -> tuple[str, ...]:
    """Render only the actions of a remedy, each as the command ``render_remedy`` would give."""
    posture = DeploymentPosture(settings.deployment_posture)
    return tuple(
        render_step(step, posture) for step in remedy.for_tenant(settings.tenant.strip()).actions
    )


DETAIL_LINE_LIMIT = 40
DETAIL_CHAR_LIMIT = 4000


def _bounded_detail(detail: str) -> str:
    """Keep the tail of a long detail block so the ``Next step:`` line stays reachable.

    A check stage can carry a whole CI log here. Rendered in full it pushes the remedy hundreds
    of lines down a Jira comment, which defeats the point of attaching one. Both bounds are
    needed because a log can be long in lines and a single line can hold a whole minified payload.
    """
    lines = detail.splitlines()
    dropped_lines = max(len(lines) - DETAIL_LINE_LIMIT, 0)
    kept = "\n".join(lines[-DETAIL_LINE_LIMIT:]) if dropped_lines else detail
    notes: list[str] = []
    if dropped_lines:
        notes.append(f"{dropped_lines} earlier lines omitted")
    if len(kept) > DETAIL_CHAR_LIMIT:
        notes.append(f"{len(kept) - DETAIL_CHAR_LIMIT} earlier characters omitted")
        kept = kept[-DETAIL_CHAR_LIMIT:]
    if not notes:
        return detail
    return f"[{', '.join(notes)}]\n{kept}"


def _intervention_message(
    alert: HumanInterventionAlert, *, issued_at: int = 0, deadline: float | None = None
) -> AlertMessage:
    body = (
        f"Human intervention required for {alert.task_id} on {alert.repo}.\n"
        f"The run stopped at the {alert.stage} stage with reason {alert.reason}.\n"
        f"Task {alert.task_id}, execution {alert.execution_id}, workflow {alert.workflow_id}.\n"
        f"{_bounded_detail(alert.detail)}"
    )
    return AlertMessage(
        subject=f"Human intervention needed on {alert.repo} for {alert.task_id}",
        body=with_remedy(body, alert.remedy),
        issue_key=alert.issue_key,
        alert=alert,
        issued_at=issued_at,
        deadline=deadline,
    )


def send_human_intervention_alert(
    alert: HumanInterventionAlert,
    settings: Settings | None = None,
    *,
    issued_at: int = 0,
    deadline: float | None = None,
) -> list[str]:
    return deliver_alert(
        _intervention_message(alert, issued_at=issued_at, deadline=deadline), settings
    )


def send_intake_unroutable_alert(
    *,
    issue_key: str,
    project: str,
    reason: str,
    remedy: str,
    settings: Settings | None = None,
) -> list[str]:
    body = f"Ticket intake stopped on {issue_key} in project {project}.\n{reason}."
    message = AlertMessage(
        subject=f"Ticket intake stopped on {issue_key}",
        body=with_remedy(body, remedy),
        issue_key=issue_key,
    )
    return deliver_alert(message, settings)
