"""Temporal activity that alerts humans when the repair loop is exhausted."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from services.operator_remedy import Remedy
from services.orchestration.activity_heartbeat import activity_seconds_left

ALERT_DELIVERY = "alert_delivery"
ALERT_IN_FLIGHT = "AlertInFlight"
PARK_BUTTONS_DELIVERY = "park_buttons_delivery"


def _alert_ledger():
    from services.coordination.idempotency import PostgresSideEffectLedger

    return PostgresSideEffectLedger()


@dataclass
class AlertHumanInterventionRequest:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    reason: str
    stage: str
    detail: str = ""
    remedy: Remedy = field(default_factory=Remedy)
    park_key: str = ""
    """The park this alert announces. Empty when the alert offers no decision to act on."""
    limit_usage: float | None = None
    """The usage of the run limit a limit park reached, which offers Request increase."""
    thread_ts: str = ""
    """The period budget alert whose thread the park's Slack post replies in."""


@dataclass
class ParkPost:
    """A Slack post of a park, which a later edit addresses and whose blocks it keeps."""

    channel: str
    ts: str
    blocks: str = ""
    admin: bool = False
    owner_token: str = ""
    thread_ts: str = ""
    """The parent post when this post is a reply, which a fresh post replies under."""


@dataclass
class AlertHumanInterventionResult:
    channels: list[str] = field(default_factory=list)
    remedy: str = ""
    """The remedy as the alert rendered it, for the audit record."""
    park_post: ParkPost | None = None
    """The tenant's Slack post of the park's buttons, or None when no post carries them."""
    issued_at: int = 0
    """When the park's buttons were signed."""
    ttl_hours: int = 0
    """The hours the park's buttons stay valid after ``issued_at``."""


@dataclass
class RefreshParkButtonsRequest:
    alert: AlertHumanInterventionRequest
    posts: list[ParkPost]
    """The park's posts, the alert post first and then each thread post in order."""
    reminder: bool = False


@dataclass
class RefreshParkButtonsResult:
    posts: list[ParkPost] = field(default_factory=list)
    """The park's posts after the refresh, with the fresh post last when one was posted."""
    issued_at: int = 0
    """When the fresh buttons were signed, or 0 when none were posted."""
    ttl_hours: int = 0


@dataclass
class SettleParkPostsRequest:
    posts: list[ParkPost]
    decision: str
    actor: str = ""


def _post_ref(post: ParkPost):
    from services.orchestration.notifications import SlackPostRef

    return SlackPostRef(post.channel, post.ts, post.admin, post.owner_token, post.blocks)


def _park_post(ref) -> ParkPost:
    return ParkPost(ref.channel, ref.ts, ref.blocks, ref.admin, ref.owner_token)


async def _limit_request(req: AlertHumanInterventionRequest, settings) -> dict[str, object]:
    from services.integration.slack.tokens import LimitRequestRef
    from services.orchestration.limit_grants import (
        RUN_LIMITS,
        PostgresLimitGrants,
        request_amounts,
    )

    if req.limit_usage is None or not req.park_key:
        return {}
    ref = LimitRequestRef(usage=req.limit_usage, park_key=req.park_key)
    if ref.limit_name not in RUN_LIMITS:
        return {}
    try:
        amounts = await request_amounts(
            settings, PostgresLimitGrants(), ref.limit_name, execution_id=req.execution_id
        )
    except Exception:  # noqa: BLE001
        activity.logger.warning(
            "the limit grants were not read, so Request increase is not offered"
        )
        return {}
    return {"limit_request": ref.alert_id, "limit_amounts": amounts} if amounts else {}


def _alert(req: AlertHumanInterventionRequest, remedy: str, limit: dict[str, object] | None = None):
    from services.orchestration.notifications import HumanInterventionAlert

    return HumanInterventionAlert(
        **(limit or {}),
        task_id=req.task_id,
        execution_id=req.execution_id,
        workflow_id=req.workflow_id,
        repo=req.repo,
        issue_key=req.issue_key,
        reason=req.reason,
        stage=req.stage,
        detail=req.detail,
        remedy=remedy,
        structured_remedy=req.remedy,
        park_key=req.park_key,
        thread_ts=req.thread_ts,
    )


def _deadline(info: activity.Info) -> float | None:
    seconds_left = activity_seconds_left(info)
    return None if seconds_left is None else time.monotonic() + seconds_left


def _ttl_hours(settings) -> int:
    from services.orchestration.notifications import tenant_slack

    slack = tenant_slack(settings)
    return slack.token_ttl_hours if slack else 0


async def _deliver_once(
    info: activity.Info,
    *,
    task_id: str,
    execution_id: str,
    operation: str,
    in_flight: str,
    deliver: Callable[[], object],
) -> object:
    """Run ``deliver`` once per activity through the alert ledger and return its recorded result."""
    from services.coordination.idempotency import CLAIM_LEASE_SECONDS, SideEffectRef

    outcome = await _alert_ledger().run_once(
        SideEffectRef(
            task_id=task_id,
            execution_id=execution_id,
            operation=operation,
            target=f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}",
        ),
        lambda: asyncio.to_thread(deliver),
    )
    if outcome.in_flight:
        raise ApplicationError(
            in_flight,
            type=ALERT_IN_FLIGHT,
            next_retry_delay=timedelta(seconds=CLAIM_LEASE_SECONDS + 5),
        )
    return outcome.result


@activity.defn
async def alert_human_intervention(
    req: AlertHumanInterventionRequest,
) -> AlertHumanInterventionResult:
    """Deliver the alert once per activity, whatever the number of attempts.

    The activity ID and run ID stay the same across retries, so an attempt after the alert was
    delivered replays the recorded channels instead of posting a second copy.
    """
    from scripts.config import get_settings

    from services.integration.slack.messages import shows_buttons
    from services.orchestration.notifications import (
        render_remedy,
        send_human_intervention_alert,
        stored_blocks,
    )

    settings = get_settings()
    remedy = render_remedy(req.remedy, settings)
    info = activity.info()
    deadline = _deadline(info)
    issued_at = int(info.scheduled_time.timestamp())
    limit = await _limit_request(req, settings)

    def deliver() -> dict[str, object]:
        posts = []
        channels = send_human_intervention_alert(
            _alert(req, remedy, limit),
            settings,
            issued_at=issued_at,
            deadline=deadline,
            posts=posts,
        )
        park_post = next(
            (post for post in posts if req.park_key and shows_buttons(stored_blocks(post.blocks))),
            None,
        )
        return {
            "channels": channels,
            "park_post": asdict(replace(_park_post(park_post), thread_ts=req.thread_ts))
            if park_post
            else None,
        }

    recorded = await _deliver_once(
        info,
        task_id=req.task_id,
        execution_id=req.execution_id,
        operation=ALERT_DELIVERY,
        in_flight="an earlier attempt of this alert is still delivering it",
        deliver=deliver,
    )
    if not isinstance(recorded, dict):
        recorded = {"channels": recorded or []}
    park_post = recorded.get("park_post")
    return AlertHumanInterventionResult(
        channels=list(recorded.get("channels") or []),
        remedy=remedy,
        park_post=ParkPost(**park_post) if park_post else None,
        issued_at=issued_at if park_post else 0,
        ttl_hours=_ttl_hours(settings) if park_post else 0,
    )


@activity.defn
async def refresh_park_buttons(req: RefreshParkButtonsRequest) -> RefreshParkButtonsResult:
    """Post the park's buttons signed anew in the alert post's thread, then take the buttons
    off the alert post and off the previous thread post, so one post of the park shows buttons.

    The post is made once per activity, whatever the number of attempts.
    """
    from scripts.config import get_settings

    from services.integration.slack.messages import PostState, shows_buttons
    from services.orchestration.notifications import (
        move_slack_post,
        post_park_buttons,
        render_remedy,
        stored_blocks,
    )

    settings = get_settings()
    info = activity.info()
    issued_at = int(info.scheduled_time.timestamp())
    alert = _alert(
        req.alert,
        render_remedy(req.alert.remedy, settings),
        await _limit_request(req.alert, settings),
    )
    deadline = _deadline(info)
    original = _post_ref(req.posts[0])
    if req.posts[0].thread_ts:
        original = replace(original, ts=req.posts[0].thread_ts)

    def deliver() -> dict[str, object] | None:
        posted = post_park_buttons(
            alert,
            settings,
            thread=original,
            issued_at=issued_at,
            reminder=req.reminder,
            deadline=deadline,
        )
        return asdict(_park_post(posted)) if posted else None

    posted = await _deliver_once(
        info,
        task_id=req.alert.task_id,
        execution_id=req.alert.execution_id,
        operation=PARK_BUTTONS_DELIVERY,
        in_flight="an earlier attempt of this refresh is still posting the buttons",
        deliver=deliver,
    )
    if not posted:
        return RefreshParkButtonsResult(posts=list(req.posts))

    def take_buttons_off() -> list[ParkPost]:
        moved = []
        for index, post in enumerate(req.posts):
            ref = _post_ref(post)
            if shows_buttons(stored_blocks(ref.blocks)):
                state = PostState.BUTTONS_REFRESHED if index == 0 else PostState.BUTTONS_SUPERSEDED
                ref = move_slack_post(ref, state, settings=settings)
            moved.append(_park_post(ref))
        return moved

    moved = await asyncio.to_thread(take_buttons_off)
    return RefreshParkButtonsResult(
        posts=[*moved, ParkPost(**posted)],
        issued_at=issued_at,
        ttl_hours=_ttl_hours(settings),
    )


@activity.defn
async def settle_park_posts(req: SettleParkPostsRequest) -> None:
    """Edit the park's alert post and each thread post that still shows buttons to the decision.

    An edit Slack refuses is logged and skipped, since the decision already stands.
    """
    from scripts.config import get_settings

    from services.integration.slack.messages import PostState, shows_buttons
    from services.orchestration.notifications import move_slack_post, stored_blocks

    settings = get_settings()
    state = PostState(req.decision)
    now = datetime.now(UTC)

    def settle() -> None:
        for index, post in enumerate(req.posts):
            ref = _post_ref(post)
            if index == 0 or shows_buttons(stored_blocks(ref.blocks)):
                move_slack_post(ref, state, settings=settings, actor=req.actor, at=now)

    await asyncio.to_thread(settle)
