"""Human alert fan-out over Jira comment, Slack, and email backup, routed by who can act.

A tenant alert names no shell command when the tenant's Slack channel is enabled. Its actions are
the buttons of its Slack post, its text names those buttons, and its Jira comment and email name
the Slack channel that holds the post. A tenant without Slack keeps the posture's commands.

A platform fault reaches the tenant as its effect and the statement that work resumes on its own,
and its cause and fix reach the administrator destination of platform-escalation.yaml, by email,
by a post to the admin Slack channel through the Slack posting service, or both. A tenant run
that fails on a platform error type also sends its cause to the administrators.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import smtplib
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from itertools import accumulate
from types import EllipsisType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError
from services.operator_remedy import (
    Remedy,
    render,
    render_for_tenant,
    render_tenant_step,
    with_remedy,
)
from services.orchestration.escalation import (
    DEFAULT_TOKEN_TTL_HOURS,
    EMAIL,
    JIRA,
    SHARED_APP,
    SLACK,
    EscalationChannel,
    EscalationConfig,
    SlackSettings,
    load_escalation_config,
)

if TYPE_CHECKING:
    from services.orchestration.platform_escalation import AdminDestination

log = logging.getLogger(__name__)


SLACK_TIMEOUT_SECONDS = 5
SLACK_MIN_SECONDS = 1.0
ADMIN_EMAIL = "admin_email"
ADMIN_SLACK = "admin_slack"


@dataclass(frozen=True)
class SlackPostRef:
    """A Slack message an alert posted, which a later edit or thread reply addresses."""

    channel: str
    ts: str
    admin: bool = False
    owner_token: str = ""
    """The posting service's owner token of an admin post, which its edits and replies present."""
    blocks: str = field(default="", compare=False)
    """The JSON of the blocks posted, which a later edit keeps."""


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
    limit_request: str = ""
    """The ``LimitRequestRef`` alert ID of a limit park whose post offers Request increase."""
    limit_amounts: tuple[float, ...] = ()
    """The in-band amounts Request increase offers."""
    thread_ts: str = ""
    """The tenant post whose thread the Slack alert replies in, or empty for a new post."""


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
    slack_text: str = ""
    slack_blocks: list[dict[str, Any]] | None = None
    """Blocks built by the sender, which Slack posts in place of the body."""
    thread_ts: str = ""
    """The message whose thread the Slack post replies in, or empty for a new post."""
    thread_owner_token: str = ""
    """The owner token of the admin post whose thread an admin reply posts in."""
    record_reply: bool = False
    """Record the thread reply in ``posts``, which a thread reply otherwise is not."""
    posts: list[SlackPostRef] = field(default_factory=list, compare=False)
    """Filled with each Slack message the delivery posted."""


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
        _smtp_send(message, settings, settings.alert_email_to)
        return True


def _smtp_send(message: AlertMessage, settings: Settings, to: str) -> None:
    email = EmailMessage()
    email["Subject"] = message.subject
    email["From"] = settings.alert_email_from
    email["To"] = to
    email.set_content(message.body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        client.send_message(email)


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
        thread = {"thread_ts": message.thread_ts} if message.thread_ts else {}
        response = client.chat_postMessage(
            channel=slack.channel,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
            **thread,
        )
        _record_post(
            message, slack.channel, response.get("ts") if response else None, blocks=blocks
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
            posted = post_message(
                settings, text=text, blocks=blocks, timeout=timeout, thread_ts=message.thread_ts
            )
        except PostingFailed as exc:
            log.warning("the alert was not posted to Slack, because %s", exc)
            return False
        _record_post(message, slack.channel, posted.ts, blocks=blocks)
        return True


def _record_post(
    message: AlertMessage,
    channel: str,
    ts: object,
    *,
    admin: bool = False,
    owner_token: str = "",
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    if isinstance(ts, str) and ts and (message.record_reply or not message.thread_ts):
        stored = json.dumps(blocks) if blocks else ""
        message.posts.append(SlackPostRef(channel, ts, admin, owner_token, stored))


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
    from services.integration.slack.messages import (
        ParkContext,
        SlackAlert,
        alert_payload,
        limit_request_elements,
    )
    from services.integration.slack.tokens import LIMIT_REQUEST_ALERT, alert_ref

    alert = message.alert
    if message.slack_blocks is not None:
        text, blocks = message.slack_text or message.subject, message.slack_blocks
    elif alert is None:
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
                detail=bounded_detail(alert.detail),
                remedy_text=alert.remedy,
                remedy=alert.structured_remedy.for_tenant(settings.tenant.strip()),
                remedy_commands=render_remedy_commands(alert.structured_remedy, settings),
                ticket_url=_ticket_url(alert.issue_key, settings),
            ),
            content=slack.content,
            park=park,
            alert_context=_run_alert_context(alert, message, settings, slack),
            limit_elements=limit_request_elements(
                alert_context(
                    settings,
                    alert_ref(LIMIT_REQUEST_ALERT, alert.limit_request),
                    issued_at=message.issued_at,
                    execution_id=alert.execution_id,
                    workflow_id=alert.workflow_id,
                    slack=slack,
                )
                if alert.limit_request and park is not None
                else None,
                alert.limit_amounts,
            )
            + _pause_jobs_button(alert, message, settings, slack),
        )
    return text, blocks


def alert_context(
    settings: Settings,
    alert: str,
    *,
    issued_at: int = 0,
    execution_id: str = "",
    workflow_id: str = "",
    slack: SlackSettings | None = None,
) -> Any:
    """Return what an alert's buttons are signed with, or None without an action key.

    ``slack`` is the tenant's Slack settings when the caller has read them.
    """
    from services.integration.slack.messages import AlertContext

    if not (alert and settings.escalation_action_key):
        return None
    slack = slack or tenant_slack(settings)
    return AlertContext(
        tenant=settings.tenant.strip(),
        alert=alert,
        issued_at=issued_at or int(time.time()),
        ttl_hours=slack.token_ttl_hours if slack else DEFAULT_TOKEN_TTL_HOURS,
        action_key=settings.escalation_action_key.encode(),
        execution_id=execution_id,
        workflow_id=workflow_id,
    )


def _run_alert_context(
    alert: HumanInterventionAlert, message: AlertMessage, settings: Settings, slack: SlackSettings
) -> Any:
    from services.integration.slack.tokens import RUN_ALERT, alert_ref

    if not alert.execution_id:
        return None
    return alert_context(
        settings,
        alert_ref(RUN_ALERT, alert.execution_id),
        issued_at=message.issued_at,
        execution_id=alert.execution_id,
        workflow_id=alert.workflow_id,
        slack=slack,
    )


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


def _enabled_slack(config: EscalationConfig) -> SlackSettings | None:
    return config.slack if SLACK in config.channels else None


def tenant_slack(settings: Settings) -> SlackSettings | None:
    """Return the tenant's Slack settings while its escalation config enables Slack."""
    return _enabled_slack(_escalation_config(settings))


def slack_pointer(slack: SlackSettings) -> str:
    return (
        f"Slack: This alert is posted in Slack channel {slack.channel}, and the buttons of that "
        "post take its actions."
    )


def deliver_alert(
    message: AlertMessage,
    settings: Settings | None = None,
    *,
    skip: frozenset[str] = frozenset(),
) -> list[str]:
    """Deliver to the tenant's enabled channels other than ``skip``, and return those reached.

    With Slack enabled, the Jira comment and the email name the Slack channel of the post.
    """
    settings = settings or get_settings()
    config = _escalation_config(settings)
    pointed = message
    slack = _enabled_slack(config)
    if slack is not None and SLACK not in skip:
        pointed = replace(message, body=f"{message.body}\n{slack_pointer(slack)}")
    delivered: list[str] = []
    for channel in CHANNELS:
        if channel.name not in config.channels or channel.name in skip:
            continue
        try:
            outgoing = message if channel.name == SLACK else pointed
            if channel.deliver(outgoing, settings, config):
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
    """Render a remedy for the tenant who reads this deployment's alerts.

    With the tenant's Slack channel enabled, each action names the button of the Slack post and
    no command. Without it, each action renders as the command of the deployment's posture. The
    worker that delivers an alert serves the run's tenant, so its TENANT is bound into every
    action that names none.
    """
    from services.validation.tenant import posture

    bound = remedy.for_tenant(settings.tenant.strip())
    if tenant_slack(settings) is not None:
        return render_for_tenant(bound)
    return render(bound, posture(settings))


def render_admin_remedy(remedy: Remedy, settings: Settings) -> str:
    """Render a remedy as the commands of the deployment's posture, for the administrators."""
    from services.validation.tenant import posture

    return render(remedy.for_tenant(settings.tenant.strip()), posture(settings))


def render_remedy_commands(remedy: Remedy, settings: Settings) -> tuple[str, ...]:
    """Render only the actions of a remedy, each as the tenant takes it in Slack."""
    return tuple(
        render_tenant_step(step) for step in remedy.for_tenant(settings.tenant.strip()).actions
    )


DETAIL_LINE_LIMIT = 40
DETAIL_CHAR_LIMIT = 4000


_OMISSION_NOTE = re.compile(r"\[(\d+) lines and (\d+) characters omitted\]")


def _omission_note(lines: int, characters: int) -> str:
    return f"[{lines} lines and {characters} characters omitted]"


def _split_at_note(lines: list[str], line_limit: int) -> tuple[list[str], list[str], int, int]:
    """Split ``lines`` into a head and a tail with the counts an earlier omission note carries.

    Detail bounded once and bounded again, as a Slack section does, keeps its one note and adds
    the second cut to its counts.
    """
    for index, line in enumerate(lines):
        if note := _OMISSION_NOTE.fullmatch(line.rstrip("\r\n")):
            return lines[:index], lines[index + 1 :], int(note[1]), int(note[2])
    head = min(line_limit // 4, len(lines) // 2)
    return lines[:head], lines[head:], 0, 0


def _longest_fit(text: str, budget: int, measure: Callable[[str], int], *, from_end: bool) -> int:
    """Return the most characters of ``text``, from its start or end, that measure within budget."""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[len(text) - mid :] if from_end else text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


MASKED = "[masked]"

_CREDENTIALS = (
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
_LABELLED = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[^\s\"']+"),
    re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+(?=@)"),
    re.compile(
        r"(\b[A-Z][A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)[A-Z0-9_]*\s*[=:]\s*)[^\s\"']+"
    ),
)


def mask_credentials(text: str) -> str:
    """Replace each credential-shaped value in alert text, keeping any label that precedes it.

    Alert detail carries tool output, plans, and diffs to Slack and the ticket, and none of those
    destinations may hold a secret that a command or a model happened to print.
    """
    for pattern in _CREDENTIALS:
        text = pattern.sub(MASKED, text)
    for pattern in _LABELLED:
        text = pattern.sub(lambda match: f"{match[1]}{MASKED}", text)
    return text


def bounded_detail(
    detail: str,
    *,
    line_limit: int = DETAIL_LINE_LIMIT,
    char_limit: int = DETAIL_CHAR_LIMIT,
    measure: Callable[[str], int] = len,
) -> str:
    """Keep the head and the tail of a long detail block, with one note counting what is left out.

    A check stage can carry a whole CI log here. Rendered in full it pushes the remedy hundreds
    of lines down a Jira comment, which defeats the point of attaching one. Both bounds are
    needed because a log can be long in lines and a single line can hold a whole minified payload.
    The head holds the cause a command such as ``npm ci`` prints first, and the tail holds the
    end of a test run. ``measure`` gives the rendered length the character limit applies to.
    Credentials are masked first, as `mask_credentials` describes.
    """
    detail = mask_credentials(detail)
    lines = detail.splitlines(keepends=True)
    head, tail, prior_lines, prior_chars = _split_at_note(lines, line_limit)
    tail_keep = min(len(tail), line_limit - min(len(head), line_limit // 4))
    head_keep = min(len(head), line_limit - tail_keep)
    dropped = head[head_keep:] + tail[: len(tail) - tail_keep]
    if not dropped and measure(detail) <= char_limit:
        return detail
    head, tail = head[:head_keep], tail[len(tail) - tail_keep :]
    head_text, tail_text = "".join(head), "".join(tail)

    note_bound = _omission_note(prior_lines + len(lines), prior_chars + len(detail))
    budget = char_limit - measure(note_bound) - 2
    head_budget = min(measure(head_text), max(budget // 4, budget - measure(tail_text)))
    head_size = _longest_fit(head_text, head_budget, measure, from_end=False)
    tail_cut = len(tail_text) - _longest_fit(
        tail_text, budget - head_budget, measure, from_end=True
    )

    head_starts = list(accumulate(map(len, head), initial=0))[:-1]
    cut_lines = sum(start >= head_size for start in head_starts)
    cut_lines += sum(end <= tail_cut for end in accumulate(map(len, tail)))
    note = _omission_note(
        prior_lines + len(dropped) + cut_lines,
        prior_chars + sum(map(len, dropped)) + len(head_text) - head_size + tail_cut,
    )
    kept = (head_text[:head_size].rstrip("\r\n"), note, tail_text[tail_cut:].rstrip("\r\n"))
    return "\n".join(part for part in kept if part)


def _intervention_message(
    alert: HumanInterventionAlert, *, issued_at: int = 0, deadline: float | None = None
) -> AlertMessage:
    body = (
        f"Human intervention required for {alert.task_id} on {alert.repo}.\n"
        f"The run stopped at the {alert.stage} stage with reason {alert.reason}.\n"
        f"Task {alert.task_id}, execution {alert.execution_id}, workflow {alert.workflow_id}.\n"
        f"{bounded_detail(alert.detail)}"
    )
    return AlertMessage(
        subject=f"Human intervention needed on {alert.repo} for {alert.task_id}",
        body=with_remedy(body, alert.remedy),
        issue_key=alert.issue_key,
        alert=alert,
        issued_at=issued_at,
        deadline=deadline,
    )


PLATFORM_RESUMES = (
    "The platform administrators have the fix, and work resumes on its own once it holds."
)


def _platform_effect_message(
    alert: HumanInterventionAlert, *, deadline: float | None = None
) -> AlertMessage:
    body = (
        f"A platform fault paused {alert.task_id} on {alert.repo} at the {alert.stage} stage.\n"
        f"{PLATFORM_RESUMES}"
    )
    return AlertMessage(
        subject=f"Platform fault paused {alert.task_id} on {alert.repo}",
        body=body,
        issue_key=alert.issue_key,
        deadline=deadline,
        slack_text=body,
    )


def _admin_intervention_message(
    alert: HumanInterventionAlert,
    settings: Settings,
    *,
    with_fix: bool,
    deadline: float | None = None,
) -> AlertMessage:
    tenant = _tenant_label(settings)
    body = (
        f"{'Platform fault' if with_fix else 'Run failed on a platform error'} for {tenant}.\n"
        f"The run of {alert.task_id} on {alert.repo} stopped at the {alert.stage} stage with "
        f"reason {alert.reason}.\n"
        f"Task {alert.task_id}, execution {alert.execution_id}, workflow {alert.workflow_id}.\n"
        f"{bounded_detail(alert.detail)}"
    )
    if with_fix:
        body = with_remedy(body, render_admin_remedy(alert.structured_remedy, settings))
    return AlertMessage(
        subject=f"Administrator alert for {tenant}, {alert.reason} on {alert.task_id}",
        body=body,
        issue_key="",
        deadline=deadline,
        slack_text=body,
    )


def send_human_intervention_alert(
    alert: HumanInterventionAlert,
    settings: Settings | None = None,
    *,
    issued_at: int = 0,
    deadline: float | None = None,
    posts: list[SlackPostRef] | None = None,
) -> list[str]:
    """Alert the tenant, and the administrators when the alert is theirs.

    ``posts`` receives each Slack post the tenant alert made.
    """
    from services.orchestration.alert_routing import Audience, alert_audience

    settings = settings or get_settings()
    audience = alert_audience(alert.reason, alert.detail)
    if audience is Audience.TENANT:
        tenant_message = _intervention_message(alert, issued_at=issued_at, deadline=deadline)
    else:
        deliver_admin_alert(
            _admin_intervention_message(
                alert, settings, with_fix=audience is Audience.PLATFORM, deadline=deadline
            ),
            settings,
        )
        tenant_message = (
            _platform_effect_message(alert, deadline=deadline)
            if audience is Audience.PLATFORM
            else _intervention_message(alert, issued_at=issued_at, deadline=deadline)
        )
    if alert.thread_ts:
        tenant_message = replace(tenant_message, thread_ts=alert.thread_ts, record_reply=True)
    delivered = deliver_alert(tenant_message, settings)
    if posts is not None:
        posts.extend(tenant_message.posts)
    return delivered


def _pause_jobs_button(
    alert: HumanInterventionAlert, message: AlertMessage, settings: Settings, slack: Any
) -> list[dict[str, Any]]:
    from services.integration.slack.messages import alert_buttons
    from services.integration.slack.tokens import PAUSE_JOBS, TENANT_JOBS_ALERT, alert_ref

    if not (alert.limit_request and alert.limit_amounts and alert.park_key):
        return []
    context = alert_context(
        settings,
        alert_ref(TENANT_JOBS_ALERT, f"run:{alert.execution_id}"),
        issued_at=message.issued_at,
        slack=slack,
    )
    return alert_buttons(context, (PAUSE_JOBS,))


def post_park_buttons(
    alert: HumanInterventionAlert,
    settings: Settings,
    *,
    thread: SlackPostRef,
    issued_at: int,
    reminder: bool,
    deadline: float | None = None,
) -> SlackPostRef | None:
    """Post the park's buttons, signed at ``issued_at``, in the thread of ``thread``.

    Returns the post, or None when the tenant's Slack does not post or Slack refused it.
    """
    from datetime import UTC, datetime

    from services.integration.slack.messages import fresh_buttons_blocks, shows_buttons

    config = _escalation_config(settings)
    slack = _enabled_slack(config)
    if slack is None:
        return None
    signed = _intervention_message(alert, issued_at=issued_at, deadline=deadline)
    text, blocks = _slack_message(signed, settings, slack)
    if not blocks or not shows_buttons(blocks):
        return None
    expires_at = datetime.fromtimestamp(issued_at + slack.token_ttl_hours * 3600, UTC)
    message = AlertMessage(
        subject=text,
        body=text,
        issue_key="",
        deadline=deadline,
        slack_text=text,
        slack_blocks=fresh_buttons_blocks(blocks, reminder=reminder, expires_at=expires_at),
        thread_ts=thread.ts,
        thread_owner_token=thread.owner_token,
        record_reply=True,
    )
    try:
        SlackChannel().deliver(message, settings, config)
    except Exception:  # noqa: BLE001
        log.warning(
            "the fresh buttons were not posted in the thread of %s", thread.ts, exc_info=True
        )
        return None
    return message.posts[0] if message.posts else None


def move_slack_post(
    post: SlackPostRef,
    state: Any,
    *,
    settings: Settings,
    actor: str = "",
    at: Any = None,
) -> SlackPostRef:
    """Edit ``post`` to ``state`` through the shared renderer, and return it with its new blocks.

    A post whose edit Slack refused is returned unchanged.
    """
    from services.integration.slack.messages import status_blocks

    blocks = status_blocks(stored_blocks(post.blocks), state, actor=actor, at=at)
    text = _post_title(blocks)
    if not update_slack_post(post, text=text, blocks=blocks, settings=settings):
        return post
    return replace(post, blocks=json.dumps(blocks))


def _post_title(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        text = (block.get("text") or {}).get("text")
        if isinstance(text, str) and text:
            return text
    return "Human intervention needed"


def deliver_admin_alert(
    message: AlertMessage,
    settings: Settings,
    destination: AdminDestination | None | EllipsisType = ...,
) -> list[str]:
    """Send ``message`` to the administrator destination, and return the destinations reached.

    The admin email list receives it over the worker's SMTP_HOST and ALERT_EMAIL_FROM, and the
    admin Slack channel receives it through the Slack posting service with the shared app.
    """
    from services.orchestration.platform_escalation import process_admin_destination

    target = process_admin_destination() if destination is ... else destination
    if target is None:
        return []
    delivered: list[str] = []
    if target.emails and _send_admin_email(message, settings, target.emails):
        delivered.append(ADMIN_EMAIL)
    if target.slack and _post_admin_slack(message, settings):
        delivered.append(ADMIN_SLACK)
    if not delivered:
        log.error(
            "the administrator alert %r reached no administrator. Check SMTP_HOST, "
            "ALERT_EMAIL_FROM, and SLACK_POSTING_URL of this worker.",
            message.subject,
        )
    return delivered


def _send_admin_email(message: AlertMessage, settings: Settings, emails: tuple[str, ...]) -> bool:
    if not (settings.smtp_host and settings.alert_email_from):
        return False
    try:
        _smtp_send(message, settings, ", ".join(emails))
    except OSError, smtplib.SMTPException:
        log.warning("the administrator alert was not emailed", exc_info=True)
        return False
    return True


def _post_admin_slack(message: AlertMessage, settings: Settings) -> bool:
    from services.integration.slack.poster_client import PostingFailed, post_message

    if not settings.slack_posting_url:
        return False
    timeout = _slack_timeout(message)
    if timeout is None:
        return False
    try:
        posted = post_message(
            settings,
            text=message.slack_text or message.subject,
            blocks=message.slack_blocks,
            timeout=timeout,
            thread_ts=message.thread_ts,
            admin=True,
            owner_token=message.thread_owner_token,
        )
    except PostingFailed as exc:
        log.warning("the administrator alert was not posted to Slack, because %s", exc)
        return False
    _record_post(
        message,
        "",
        posted.ts,
        admin=True,
        owner_token=posted.owner_token,
        blocks=message.slack_blocks,
    )
    return True


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


def _tenant_label(settings: Settings) -> str:
    tenant = settings.tenant.strip()
    return f"tenant {tenant}" if tenant else "this deployment"


MODEL_ERROR_SUMMARY_LIMIT = 300
_LITELLM_ERROR_CLASS = re.compile(r"^litellm\.\w+:\s*")
_SSL_CONTEXT = re.compile(r"\s*ssl:<[^>]*>(\s*\[None\])?")


def model_error_summary(detail: str) -> str:
    """Reduce a LiteLLM error answer to the provider's own message, on one bounded line."""
    text = detail
    start = detail.find("{")
    if start != -1:
        with contextlib.suppress(ValueError):
            payload = json.loads(detail[start:])
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("message"):
                text = str(error["message"])
    text = text.split("\n\n", 1)[0]
    text = _SSL_CONTEXT.sub("", _LITELLM_ERROR_CLASS.sub("", text.strip()))
    text = " ".join(text.split())
    if len(text) > MODEL_ERROR_SUMMARY_LIMIT:
        return f"{text[: MODEL_ERROR_SUMMARY_LIMIT - 3]}..."
    return text


def stored_blocks(blocks: str) -> list[dict[str, Any]]:
    """Return the blocks a post recorded, or none for a post recorded without them."""
    try:
        loaded = json.loads(blocks) if blocks else []
    except ValueError:
        return []
    return loaded if isinstance(loaded, list) else []


def update_slack_post(
    post: SlackPostRef, *, text: str, blocks: list[dict[str, Any]], settings: Settings
) -> bool:
    """Replace the text and blocks of a post an alert made, and return whether Slack took it.

    An administrator post is edited through the Slack posting service, whichever app the tenant
    uses, since only the shared app posts in the administrator channel.
    """
    slack = None if post.admin else tenant_slack(settings)
    if slack is None and not (post.admin and settings.slack_posting_url):
        return False
    try:
        if slack is None or slack.app == SHARED_APP:
            from services.integration.slack.poster_client import update_message

            update_message(
                settings,
                ts=post.ts,
                text=text,
                blocks=blocks,
                timeout=SLACK_TIMEOUT_SECONDS,
                admin=post.admin,
                owner_token=post.owner_token,
            )
        elif settings.slack_bot_token:
            from services.integration.slack.client import tenant_web_client

            tenant_web_client(
                settings.slack_bot_token,
                slack.api_base_url,
                timeout=SLACK_TIMEOUT_SECONDS,
                retry_handlers=[],
            ).chat_update(channel=post.channel, ts=post.ts, text=text, blocks=blocks)
        else:
            return False
    except Exception:  # noqa: BLE001
        log.warning("the Slack post %s was not edited", post.ts, exc_info=True)
        return False
    return True


def post_thread_reply(post: SlackPostRef, *, text: str, settings: Settings) -> bool:
    """Post ``text`` in the thread of ``post``, in the administrator channel for an admin post
    and in the tenant's Slack channel otherwise."""
    from services.integration.slack.messages import escape

    message = AlertMessage(
        subject=text,
        body=text,
        issue_key="",
        thread_ts=post.ts,
        thread_owner_token=post.owner_token,
        slack_text=text,
        slack_blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": escape(text)}}],
    )
    if post.admin:
        return _post_admin_slack(message, settings)
    config = _escalation_config(settings)
    if _enabled_slack(config) is None:
        return False
    try:
        return SlackChannel().deliver(message, settings, config)
    except Exception:  # noqa: BLE001
        log.warning("the reply in the thread of %s was not posted", post.ts, exc_info=True)
        return False
