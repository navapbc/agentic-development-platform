"""Alerts for a model path outage, its recovery, and the resolution of its Slack posts."""

from __future__ import annotations

from scripts.config import Settings

from services.orchestration import notifications
from services.orchestration.notifications import (
    PLATFORM_RESUMES,
    AlertMessage,
    SlackPostRef,
    _tenant_label,
    alert_context,
    model_error_summary,
    stored_blocks,
    with_remedy,
)

VALIDATE_LINE = "Press Run validation, and the report posts in this thread."
FIX_DONE_LINE = "Press Fix done once validation passes, and work resumes at once."


def _hop_label(hop: str) -> str:
    from services.orchestration.remedies import MODEL_PATH_HOP_LABELS

    label = MODEL_PATH_HOP_LABELS.get(hop)
    return f"{label} ({hop} hop)" if label else f"{hop or 'unknown'} hop"


def _hop_failure(hop: str) -> str:
    from services.orchestration.remedies import MODEL_PATH_FAILURES

    failure = MODEL_PATH_FAILURES.get(hop, "a hop of the model path failed")
    return failure[:1].upper() + failure[1:]


def model_path_title(settings: Settings) -> str:
    return f"Model path unavailable for {_tenant_label(settings)}"


def send_model_path_unavailable_alert(
    *, hop: str, detail: str, issue_key: str, remedy: str, settings: Settings, episode: str = ""
) -> list[SlackPostRef]:
    """Alert the tenant and the administrators once per outage, and return the Slack posts made.

    The tenant receives the effect and the statement that work resumes on its own, on the ticket
    whose pickup found the outage. The administrators receive the error and ``remedy``, the fix
    rendered for the deployment's posture. An outage found by the probe for parked runs has no
    ticket, so the tenant alert names none. Each Slack post of an ``episode`` carries Acknowledge,
    Run validation, and Fix done.
    """
    from services.integration.slack.messages import OUTAGE_ACTIONS, alert_buttons, labeled_blocks
    from services.integration.slack.tokens import MODEL_PATH_ALERT, alert_ref

    scope = _tenant_label(settings)
    title = model_path_title(settings)
    first = f" The first deferred ticket is {issue_key}." if issue_key else ""
    effect = f"Intake defers every ticket and starts no run.{first}"
    happened = f"{_hop_failure(hop)}, at the {_hop_label(hop)}."
    error = model_error_summary(detail)
    context = alert_context(settings, alert_ref(MODEL_PATH_ALERT, episode)) if episode else None
    buttons = alert_buttons(context, OUTAGE_ACTIONS)
    validate = f"{VALIDATE_LINE} {FIX_DONE_LINE}" if buttons else ""
    summary = (
        f"{title}\nFailing hop: {_hop_label(hop)}\nCause: {_hop_failure(hop)}.\nEffect: {effect}"
    )
    rows = [("What happened", happened), ("Effect", effect)]
    tenant = AlertMessage(
        subject=f"Model path unavailable for {scope}, {_hop_label(hop)}",
        body=f"{summary}\n{PLATFORM_RESUMES}",
        issue_key=issue_key,
        slack_text=title,
        slack_blocks=labeled_blocks(
            title,
            [
                *rows,
                ("Status", PLATFORM_RESUMES),
                ("How to validate", validate),
            ],
            buttons,
        ),
    )
    admin = AlertMessage(
        subject=f"Administrator alert, model path unavailable for {scope}, {_hop_label(hop)}",
        body=with_remedy(f"{summary}\nError: {error}", remedy),
        issue_key="",
        slack_text=title,
        slack_blocks=labeled_blocks(
            title,
            [
                *rows,
                ("Error", error),
                ("How to fix", remedy),
                ("How to validate", validate),
            ],
            buttons,
        ),
    )
    notifications.deliver_alert(tenant, settings)
    notifications.deliver_admin_alert(admin, settings)
    return [*tenant.posts, *admin.posts]


def send_model_path_recovered_alert(
    *,
    hop: str,
    issue_key: str,
    remedy: str,
    settings: Settings,
    outage_post: SlackPostRef | None = None,
    admin_post: SlackPostRef | None = None,
    resolver: str = "",
) -> list[str]:
    """Send the recovery to the tenant and to the administrators, each as a reply in the thread
    of its outage post when there is one, and edit each outage post to resolved.

    The administrators receive the recovery once, so an admin email list receives one email.
    """
    scope = _tenant_label(settings)
    first = f" The first ticket it starts is {issue_key}." if issue_key else ""
    body = (
        f"Model path recovered for {scope}\n"
        f"Recovered hop: {_hop_label(hop)}\n"
        f"Effect: Intake starts the deferred tickets and resumes the runs parked on the model "
        f"path.{first}"
    )
    message = AlertMessage(
        subject=f"Model path recovered for {scope}",
        body=with_remedy(body, remedy),
        issue_key=issue_key,
        thread_ts=outage_post.ts if outage_post else "",
    )
    delivered = notifications.deliver_alert(message, settings)
    admin = AlertMessage(
        subject=f"Administrator alert, model path recovered for {scope}",
        body=f"{body}\nResolution: {_resolution(resolver)}",
        issue_key="",
        thread_ts=admin_post.ts if admin_post else "",
        thread_owner_token=admin_post.owner_token if admin_post else "",
    )
    delivered += notifications.deliver_admin_alert(admin, settings)
    for post in (outage_post, admin_post):
        if post is not None:
            _resolve_outage_post(post, hop=hop, resolver=resolver, settings=settings)
    return delivered


INTAKE_RESOLUTION = "Intake found the path healthy."
FIX_DONE_PRESSED = "Fix done pressed by"
FIX_DONE_NOTE = "Fix done pressed."


def _resolution(resolver: str) -> str:
    from services.integration.slack.messages import actor_words

    return f"{FIX_DONE_PRESSED} {actor_words(resolver)}." if resolver else INTAKE_RESOLUTION


def _resolve_outage_post(
    post: SlackPostRef, *, hop: str, resolver: str, settings: Settings
) -> None:
    from datetime import UTC, datetime

    from services.integration.slack.messages import PostState, labeled_blocks, status_blocks

    title = model_path_title(settings)
    original = stored_blocks(post.blocks) or labeled_blocks(
        title, [("Failing hop", _hop_label(hop))], []
    )
    blocks = status_blocks(
        original,
        PostState.RESOLVED,
        actor=resolver,
        at=datetime.now(UTC),
        note=FIX_DONE_NOTE if resolver else INTAKE_RESOLUTION,
    )
    notifications.update_slack_post(post, text=title, blocks=blocks, settings=settings)
