"""Block Kit messages for alerts, with buttons for the decisions and actions the alert offers.

A park's buttons decide the park. An alert's buttons act on the alert, Acknowledge, Run
validation, Fix done, and Restart run, and are signed for the alert rather than a park. The
blocks carry block IDs, so ``status_blocks``, which renders every state change of a post, can
remove the buttons or replace one state's status line without parsing the text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    DENY_LIMIT,
    FIX_DONE,
    GRANT_LIMIT,
    PAUSE_JOBS,
    REQUEST_LIMIT,
    RESTART,
    RESUME_JOBS,
    VALIDATE,
    ActionToken,
    sign_action_token,
)
from services.operator_remedy import (
    ACKNOWLEDGE_LABEL,
    BUTTON_LABELS,
    FIX_DONE_LABEL,
    VALIDATE_LABEL,
    Remedy,
    StepKind,
)
from services.orchestration.escalation import GATE_KIND, MINIMAL_CONTENT, REPAIR_KIND
from services.orchestration.notifications import bounded_detail

SECTION_TEXT_LIMIT = 3000
"""Slack refuses a message whose section text is longer than this."""

_BUTTONS: dict[str, tuple[str, str, str, str]] = {
    StepKind.APPROVE_GATE: ("approve", GATE_KIND, "Approve", "primary"),
    StepKind.REJECT_GATE: ("reject", GATE_KIND, "Reject", "danger"),
    StepKind.RETRY_REPAIR: ("retry", REPAIR_KIND, "Retry repair", "primary"),
    StepKind.ABANDON_REPAIR: ("abandon", REPAIR_KIND, "Abandon", "danger"),
}


ACTIONS_BLOCK = "adp-actions"

_ALERT_BUTTONS: dict[str, tuple[str, str | None]] = {
    ACKNOWLEDGE: (ACKNOWLEDGE_LABEL, None),
    VALIDATE: (VALIDATE_LABEL, None),
    FIX_DONE: (FIX_DONE_LABEL, "primary"),
    RESTART: (BUTTON_LABELS[StepKind.RESTART_RUN], "primary"),
    GRANT_LIMIT: ("Approve", "primary"),
    DENY_LIMIT: ("Deny", "danger"),
    PAUSE_JOBS: ("Park all jobs", "danger"),
    RESUME_JOBS: ("Resume jobs", "primary"),
}
OUTAGE_ACTIONS = (ACKNOWLEDGE, VALIDATE, FIX_DONE)
"""The buttons of a model path or platform fault alert."""
RUN_ACTIONS = (ACKNOWLEDGE, RESTART)
"""The buttons of an alert about a run the tenant can restart."""
LIMIT_ACTIONS = (GRANT_LIMIT, DENY_LIMIT)
"""The administrator buttons of a limit increase request."""
REQUEST_LIMIT_LABEL = "Request increase"
LIMIT_NEXT_STEP = (
    "Choose an amount under Request increase to ask the administrators for more of this limit, "
    "press Acknowledge to show the alert is seen, or press Abandon to end the run."
)


@dataclass(frozen=True)
class AlertContext:
    """What an alert's buttons are signed for: the tenant, the alert, and the run it names."""

    tenant: str
    alert: str
    issued_at: int
    ttl_hours: int
    action_key: bytes
    execution_id: str = ""
    workflow_id: str = ""


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
    """The remedy's actions as the tenant takes them, without its prose, which can carry run
    detail."""
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


SLACK_ACTOR_PREFIX = "slack:"


def slack_user(actor: str) -> str:
    """Return the user ID of a ``slack:<organization>/<user>`` actor, or empty for another actor."""
    if not actor.startswith(SLACK_ACTOR_PREFIX):
        return ""
    return actor.rpartition("/")[2]


def mention(user: str) -> str:
    return f"<@{escape(user)}>"


def actor_words(actor: str) -> str:
    """Name ``actor`` for email and Jira text, where a Slack mention does not render."""
    user = slack_user(actor)
    return f"Slack user {user}" if user else actor


def _detail_text(detail: str) -> str:
    """Fence the detail, bounded so that it fits one section once escaped."""
    fence = "```"
    bounded = bounded_detail(
        detail,
        char_limit=SECTION_TEXT_LIMIT - 2 * len(fence),
        measure=lambda text: len(escape(text)),
    )
    return f"{fence}{escape(bounded)}{fence}"


def _issue_ref(alert: SlackAlert) -> str:
    """Return the bold issue key, linked to its ticket when the URL is a safe HTTPS link."""
    key = escape(alert.issue_key)
    url = alert.ticket_url
    if url.startswith("https://") and not any(char in url for char in "|<> "):
        return f"*<{url}|{key}>*"
    return f"*{key}*"


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(action: str, label: str, style: str | None, value: str) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def _sign_alert(context: AlertContext, actions: tuple[str, ...]) -> str | None:
    token = ActionToken(
        tenant=context.tenant,
        execution_id=context.execution_id,
        workflow_id=context.workflow_id,
        park_key="",
        kind=REPAIR_KIND,
        actions=actions,
        expires_at=context.issued_at + context.ttl_hours * 3600,
        alert=context.alert,
    )
    try:
        return sign_action_token(token, context.action_key)
    except ValueError:
        return None


def alert_buttons(context: AlertContext | None, actions: Sequence[str]) -> list[dict[str, Any]]:
    """Return a signed button for each alert action, or none when the alert cannot be signed."""
    if context is None or not context.alert:
        return []
    buttons = []
    for action in actions:
        label, style = _ALERT_BUTTONS[action]
        value = _sign_alert(context, (action,))
        if value is None:
            return []
        buttons.append(_button(action, label, style, value))
    return buttons


def limit_request_elements(
    context: AlertContext | None, amounts: Sequence[float]
) -> list[dict[str, Any]]:
    """Return the Request increase select over ``amounts`` and the Acknowledge button.

    Slack sends a select's option value without a signed value of its own, so the Acknowledge
    button's token also permits Request increase and the dispatcher reads it from this message.
    Each option value carries only the amount.
    """
    if context is None or not context.alert or not amounts:
        return []
    value = _sign_alert(context, (ACKNOWLEDGE, REQUEST_LIMIT))
    if value is None:
        return []
    options = [
        {"text": {"type": "plain_text", "text": f"{amount:g}"}, "value": f"{amount:g}"}
        for amount in amounts
    ]
    select = {
        "type": "static_select",
        "action_id": REQUEST_LIMIT,
        "placeholder": {"type": "plain_text", "text": REQUEST_LIMIT_LABEL},
        "options": options,
    }
    return [select, _button(ACKNOWLEDGE, ACKNOWLEDGE_LABEL, None, value)]


def actions_block(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "actions", "block_id": ACTIONS_BLOCK, "elements": buttons}] if buttons else []


def labeled_blocks(
    title: str, lines: Sequence[tuple[str, str]], buttons: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    text = "\n".join(f"*{escape(label)}:* {escape(value)}" for label, value in lines if value)
    blocks = title_blocks(title)
    if text:
        blocks.append(_section(text))
    return blocks + actions_block(buttons)


def _context(block_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "context",
        "block_id": block_id,
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


class PostState(StrEnum):
    """A state a post moves to. The values of the decision states are the recorded decisions."""

    ACKNOWLEDGED = "acknowledged"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETRY_REQUESTED = "retry"
    ABANDONED = "abandon"
    RESOLVED = "resolved"
    RESTARTED = "restarted"
    GRANTED = "granted"
    DENIED = "denied"
    JOBS_PAUSED = "jobs_paused"
    JOBS_RESUMED = "jobs_resumed"
    PERIOD_RESET = "period_reset"
    BUTTONS_REFRESHED = "buttons_refreshed"
    """The park's buttons moved to a newer post in this post's thread."""
    BUTTONS_SUPERSEDED = "buttons_superseded"
    """A thread post's buttons moved to a later post in the same thread."""

    @property
    def block_id(self) -> str:
        return f"adp-{self.value}"


ACKNOWLEDGED_BLOCK = PostState.ACKNOWLEDGED.block_id
RESOLVED_BLOCK = PostState.RESOLVED.block_id

_STATE_LABELS = {
    PostState.ACKNOWLEDGED: "Acknowledged",
    PostState.APPROVED: "Approved",
    PostState.REJECTED: "Rejected",
    PostState.RETRY_REQUESTED: "Retry requested",
    PostState.ABANDONED: "Abandoned",
    PostState.RESOLVED: "Resolved",
    PostState.RESTARTED: "Restarted",
    PostState.GRANTED: "Granted",
    PostState.DENIED: "Denied",
    PostState.JOBS_PAUSED: "Jobs parked",
    PostState.JOBS_RESUMED: "Jobs resumed",
    PostState.PERIOD_RESET: "The budget period reset",
    PostState.BUTTONS_REFRESHED: (
        "These buttons expired. Fresh buttons are posted in this message's thread"
    ),
    PostState.BUTTONS_SUPERSEDED: (
        "These buttons were replaced. Newer buttons follow in this thread"
    ),
}
_KEEPS_BUTTONS = frozenset({PostState.ACKNOWLEDGED})


def actor_mrkdwn(actor: str) -> str:
    user = slack_user(actor)
    return mention(user) if user else escape(actor)


def title_blocks(title: str) -> list[dict[str, Any]]:
    return [_section(f"*{escape(title)}*")]


def status_line(
    state: PostState, *, actor: str = "", at: datetime | None = None, note: str = ""
) -> str:
    """Return the mrkdwn of a post's status line, such as ``Approved by <@U1> at <time>.``

    The prose is escaped and the actor is rendered after escaping, so only the actor can mention.
    """
    line = escape(_STATE_LABELS[state])
    if actor:
        line += f" by {actor_mrkdwn(actor)}"
    if at is not None:
        line += f" at {_stamp(at)}"
    line += "."
    return f"{line} {escape(note)}" if note else line


def status_blocks(
    blocks: Sequence[dict[str, Any]],
    state: PostState,
    *,
    actor: str = "",
    at: datetime | None = None,
    note: str = "",
    keep: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Return the post's ``blocks`` moved to ``state``, with every content block kept.

    The state's status line replaces an earlier line of the same state and leaves the lines of
    other states in place. A state that keeps the buttons puts its line above them, and every
    other state removes them and appends its line. ``keep`` names the action IDs a state that
    removes the buttons leaves in place, under its line.
    """
    line = _context(state.block_id, status_line(state, actor=actor, at=at, note=note))
    kept = [block for block in blocks if block.get("block_id") != state.block_id]
    if keep and state not in _KEEPS_BUTTONS:
        kept = [_kept_actions(block, keep) for block in kept]
        kept = [block for block in kept if block.get("type") != "actions" or block["elements"]]
        if not shows_buttons(kept):
            return [*kept, line]
    elif state not in _KEEPS_BUTTONS:
        return [*(block for block in kept if block.get("type") != "actions"), line]
    for index, block in enumerate(kept):
        if block.get("block_id") == ACTIONS_BLOCK or block.get("type") == "actions":
            return [*kept[:index], line, *kept[index:]]
    return [*kept, line]


def _kept_actions(block: dict[str, Any], keep: frozenset[str]) -> dict[str, Any]:
    if block.get("type") != "actions":
        return block
    elements = [
        element for element in block.get("elements") or () if element.get("action_id") in keep
    ]
    return {**block, "elements": elements}


def shows_buttons(blocks: Sequence[dict[str, Any]]) -> bool:
    return any(block.get("type") == "actions" for block in blocks)


def fresh_buttons_blocks(
    blocks: Sequence[dict[str, Any]], *, reminder: bool, expires_at: datetime
) -> list[dict[str, Any]]:
    """Return the buttons of an alert's ``blocks`` under a line naming when they expire."""
    lead = "This run still waits for a decision. " if reminder else ""
    intro = _section(f"{lead}These buttons expire at {_stamp(expires_at)}.")
    return [intro, *(block for block in blocks if block.get("type") == "actions")]


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
        buttons.append(_button(action, label, style, value))
    return buttons


def _run_actions(remedy: Remedy, park: ParkContext | None) -> tuple[str, ...]:
    if park is not None and park.park_key:
        return ()
    restart = any(step.kind == StepKind.RESTART_RUN for step in remedy.actions)
    return RUN_ACTIONS if restart else ()


def alert_payload(
    alert: SlackAlert,
    *,
    content: str,
    park: ParkContext | None,
    alert_context: AlertContext | None = None,
    limit_elements: Sequence[dict[str, Any]] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Return the notification text and the blocks of the alert message.

    ``minimal`` content carries the issue key linked to its ticket, the stage, the reason code,
    and the remedy's actions, and leaves out the repository, the failure detail, and the
    remedy's prose. A limit park's ``limit_elements`` put Request increase and Acknowledge
    before Abandon, and its next step names those buttons in place of the remedy.
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
        next_step = (
            f"Press {', or '.join(alert.remedy_commands)}."
            if alert.remedy_commands
            else f"The full next step is on the {alert.issue_key} alert in Jira or email."
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
    if limit_elements:
        next_step = LIMIT_NEXT_STEP
    if next_step:
        blocks.append(_section(f"*Next step:* {escape(next_step)}"))
    buttons = (
        list(limit_elements)
        + _buttons(alert.remedy, park)
        + alert_buttons(alert_context, _run_actions(alert.remedy, park))
    )
    return text, blocks + actions_block(buttons)


def ephemeral_reply(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}
