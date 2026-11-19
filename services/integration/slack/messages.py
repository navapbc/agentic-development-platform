"""Block Kit messages for a parked run, with buttons for the decisions the remedy names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.integration.slack.tokens import ActionToken, sign_action_token
from services.operator_remedy import Remedy, StepKind
from services.orchestration.escalation import GATE_KIND, MINIMAL_CONTENT, REPAIR_KIND

SECTION_TEXT_LIMIT = 3000
"""Slack refuses a message whose section text is longer than this."""

_BUTTONS: dict[str, tuple[str, str, str, str]] = {
    StepKind.APPROVE_GATE: ("approve", GATE_KIND, "Approve", "primary"),
    StepKind.REJECT_GATE: ("reject", GATE_KIND, "Reject", "danger"),
    StepKind.RETRY_REPAIR: ("retry", REPAIR_KIND, "Retry repair", "primary"),
    StepKind.ABANDON_REPAIR: ("abandon", REPAIR_KIND, "Abandon", "danger"),
}


@dataclass(frozen=True)
class SlackAlert:
    task_id: str
    repo: str
    issue_key: str
    stage: str
    reason: str
    detail: str
    remedy_text: str
    remedy: Remedy
    remedy_commands: tuple[str, ...] = ()
    """The rendered commands of the remedy, without its prose, which can carry run detail."""
    ticket_url: str = ""


@dataclass(frozen=True)
class ParkContext:
    tenant: str
    execution_id: str
    workflow_id: str
    park_key: str
    issued_at: int
    ttl_hours: int
    action_key: bytes


def escape(text: str) -> str:
    """Escape the three characters Slack reads as markup, so text cannot mention or link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _detail_text(detail: str) -> str:
    """Fence the tail of the detail that fits one section once escaped."""
    kept = len(detail)
    while kept > 0 and len(escape(detail[-kept:])) + 6 > SECTION_TEXT_LIMIT:
        kept -= 100
    return f"```{escape(detail[-kept:] if kept > 0 else '')}```"


def _issue_ref(alert: SlackAlert) -> str:
    """Return the bold issue key, linked to its ticket when the URL is a safe HTTPS link."""
    key = escape(alert.issue_key)
    url = alert.ticket_url
    if url.startswith("https://") and not any(char in url for char in "|<> "):
        return f"*<{url}|{key}>*"
    return f"*{key}*"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _buttons(remedy: Remedy, park: ParkContext | None) -> list[dict[str, Any]]:
    if park is None or not park.park_key:
        return []
    buttons = []
    for step in remedy.actions:
        spec = _BUTTONS.get(step.kind)
        if spec is None:
            continue
        action, kind, label, style = spec
        token = ActionToken(
            tenant=park.tenant,
            execution_id=park.execution_id,
            workflow_id=park.workflow_id,
            park_key=park.park_key,
            kind=kind,
            actions=(action,),
            expires_at=park.issued_at + park.ttl_hours * 3600,
        )
        try:
            value = sign_action_token(token, park.action_key)
        except ValueError:
            return []
        buttons.append(
            {
                "type": "button",
                "action_id": action,
                "text": {"type": "plain_text", "text": label},
                "style": style,
                "value": value,
            }
        )
    return buttons


def alert_payload(
    alert: SlackAlert, *, content: str, park: ParkContext | None
) -> tuple[str, list[dict[str, Any]]]:
    """Return the notification text and the blocks of the alert message.

    ``minimal`` content carries the issue key linked to its ticket, the stage, the reason code,
    and the remedy's commands, and leaves out the repository, the failure detail, and the
    remedy's prose.
    """
    text = (
        f"Human intervention needed on {escape(alert.issue_key)} at the {escape(alert.stage)} stage"
    )
    if content == MINIMAL_CONTENT:
        summary = (
            f"{_issue_ref(alert)} parked at the {escape(alert.stage)} stage "
            f"with reason `{escape(alert.reason)}`."
        )
        blocks = [_section(summary)]
        next_step = " ".join(alert.remedy_commands) or (
            f"The full next step is on the {alert.issue_key} alert in Jira or email."
        )
    else:
        summary = (
            f"{_issue_ref(alert)} on {escape(alert.repo)} parked at the "
            f"{escape(alert.stage)} stage with reason `{escape(alert.reason)}`."
        )
        blocks = [_section(summary)]
        if alert.detail.strip():
            blocks.append(_section(_detail_text(alert.detail)))
        next_step = alert.remedy_text
    if next_step:
        blocks.append(_section(f"*Next step:* {escape(next_step)}"))
    buttons = _buttons(alert.remedy, park)
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    return text, blocks


def ephemeral_reply(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}
