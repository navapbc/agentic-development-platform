"""Take the action a tenant's alert button asks for, in the tenant worker.

The dispatcher has verified the click, admitted its user under the ``repair`` kind, and named
the basis. A model path alert takes Acknowledge, Run validation, and Fix done against the open
outage episode, and a click on a resolved episode or on an earlier post of the outage is refused.
A run alert takes Acknowledge and Restart run, and the restart runs through the side-effect
ledger keyed on the execution, so it restarts the run once however many clicks reach it. Every
action taken is audited with the actor and the basis.

A click from the platform administrator channel acts on the administrator post it was made on.
Run validation runs the platform validation and posts its report in that post's thread. Fix done
resolves the episode, whose recovery edits the tenant post and the administrator post the episode
recorded, and the clicked post when the episode recorded none. The inbound webhook does not hold the
tenant's credentials, so ForwardedAlertActions hands each click it admitted to the tenant's
worker as an AlertActionWorkflow on the tenant's task queue. An administrator's Approve or Deny on a
limit increase alert is taken by LimitGrantDecisions.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.integration.slack.dispatcher import (
    AlertActions,
    AlertOutcome,
    AlertRequest,
    Refusal,
)
from services.integration.slack.messages import (
    PostState,
    actor_words,
    shows_buttons,
    status_blocks,
    title_blocks,
)
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    FIX_DONE,
    LIMIT_ALERT,
    LIMIT_REQUEST_ALERT,
    MODEL_PATH_ALERT,
    RESTART,
    RUN_ALERT,
    TENANT_JOBS_ALERT,
    VALIDATE,
)
from services.orchestration.model_path import (
    ModelPathEpisode,
    ModelPathEpisodes,
    ModelPathGate,
    OutagePost,
)
from services.orchestration.model_path_alerts import model_path_title
from services.orchestration.notifications import (
    SlackPostRef,
    model_error_summary,
    post_thread_reply,
    stored_blocks,
    update_slack_post,
)
from services.orchestration.temporal_activities.persistence import AuditEvent

log = structlog.get_logger("alert_actions")

ALERT_ACTION_EVENT = "SLACK_ALERT_ACTION"
RESTART_OPERATION = "slack_restart_run"
RESOLVED_LINE = "This alert is resolved."
FORWARD_BOUND_SECONDS = 2.5
FORWARD_TIMEOUT_MINUTES = 10
_FORWARDED = (
    "The tenant worker is taking this action. Its outcome appears on the alert post and in its "
    "thread."
)
_NOT_STARTED = (
    "The tenant worker has not picked up this action yet, which happens while it restarts. If the "
    "alert post does not change within a minute, press the button again."
)


@dataclass(frozen=True)
class RestartOutcome:
    restarted: bool
    message: str


class RestartNotTaken(Exception):
    """The restart did not start a run, so the ledger keeps the restart open for a later click."""


class SlackPosts:
    """Edit a post and reply in its thread through the tenant's Slack channel."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def update(self, post: SlackPostRef, *, text: str, blocks: list[dict[str, Any]]) -> bool:
        return await asyncio.to_thread(
            update_slack_post, post, text=text, blocks=blocks, settings=self._settings
        )

    async def reply(self, post: SlackPostRef, *, text: str) -> bool:
        return await asyncio.to_thread(post_thread_reply, post, text=text, settings=self._settings)


class TenantAlertActions:
    def __init__(
        self,
        *,
        settings: Any,
        gate: ModelPathGate | None,
        episodes: ModelPathEpisodes,
        ledger: SideEffectLedger,
        audit: Callable[[AuditEvent], Awaitable[Any]],
        posts: Any,
        restart: Callable[[str, str], Awaitable[RestartOutcome]],
        validate: Callable[[], Awaitable[str]],
        trigger_intake: Callable[[], Awaitable[None]],
        validate_platform: Callable[[], Awaitable[str]] | None = None,
        limits: AlertActions | None = None,
        limit_requests: AlertActions | None = None,
        tenant_jobs: AlertActions | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._tenant = settings.tenant.strip()
        self._gate = gate
        self._episodes = episodes
        self._ledger = ledger
        self._audit = audit
        self._posts = posts
        self._restart = restart
        self._validate = validate
        self._trigger_intake = trigger_intake
        self._validate_platform = validate_platform or validate
        self._limits = limits
        self._limit_requests = limit_requests
        self._tenant_jobs = tenant_jobs
        self._clock = clock

    async def act(self, request: AlertRequest) -> AlertOutcome:
        if request.subject == MODEL_PATH_ALERT:
            return await self._model_path(request)
        if request.subject == RUN_ALERT and request.alert_id == request.execution_id:
            return await self._run(request)
        if request.subject == LIMIT_ALERT and self._limits is not None:
            return await self._limits.act(request)
        if request.subject == LIMIT_REQUEST_ALERT and not request.admin:
            return await self._limit_request(request, self._limit_requests)
        if request.subject == TENANT_JOBS_ALERT and not request.admin:
            return await self._limit_request(request, self._tenant_jobs)
        return AlertOutcome(refusal=Refusal.NOT_PERMITTED)

    async def _limit_request(
        self, request: AlertRequest, actions: AlertActions | None
    ) -> AlertOutcome:
        if request.action == ACKNOWLEDGE:
            post = SlackPostRef(request.channel, request.message_ts)
            await self._acknowledge_post(request, post, self._clock())
            await self._record(request, "acknowledged")
            return AlertOutcome(reply=f"Acknowledged by <@{request.user}>.", applied=True)
        if actions is None:
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
        return await actions.act(request)

    async def _model_path(self, request: AlertRequest) -> AlertOutcome:
        episode = await self._episodes.current(self._tenant)
        if episode is None or episode.episode != request.alert_id:
            await self._resolve_clicked_post(request)
            return AlertOutcome(refusal=Refusal.RESOLVED)
        if request.admin:
            owner_token = (
                episode.admin_slack_token if request.message_ts == episode.admin_slack_ts else ""
            )
            post = SlackPostRef(
                request.channel, request.message_ts, admin=True, owner_token=owner_token
            )
        elif request.message_ts and episode.slack_ts and request.message_ts != episode.slack_ts:
            return AlertOutcome(refusal=Refusal.EARLIER_POST)
        else:
            post = SlackPostRef(
                episode.slack_channel or request.channel, episode.slack_ts or request.message_ts
            )
        if request.action == ACKNOWLEDGE:
            at = self._clock()
            if await self._episodes.acknowledge(self._tenant, episode.episode, request.actor, at):
                stored = _post_blocks(episode, post)
                blocks = await self._acknowledge_post(request, post, at, stored)
                if blocks and stored is not None:
                    await self._episodes.record_post(
                        self._tenant,
                        episode.episode,
                        OutagePost(
                            post.channel, post.ts, post.admin, post.owner_token, json.dumps(blocks)
                        ),
                    )
                await self._record(request, "acknowledged")
                return AlertOutcome(reply=f"Acknowledged by <@{request.user}>.", applied=True)
            return AlertOutcome(refusal=Refusal.RESOLVED)
        if request.action == VALIDATE:
            report = await (self._validate_platform if request.admin else self._validate)()
            await self._posts.reply(post, text=report)
            await self._record(request, "validated")
            return AlertOutcome(
                reply="The validation report is posted in the thread of this alert.", applied=True
            )
        if request.action == FIX_DONE and self._gate is not None:
            health = await self._gate.confirm_fix(resolver=request.actor)
            await self._record(request, "resolved" if health.healthy else "still_failing")
            if not health.healthy:
                return AlertOutcome(
                    reply=(
                        f"The model path still fails at the {health.hop} hop. "
                        f"{model_error_summary(health.error)}"
                    )
                )
            if request.admin and not episode.admin_slack_ts and post.ts and request.message_blocks:
                await self._posts.update(
                    post,
                    text=f"Resolved by {actor_words(request.actor)}",
                    blocks=status_blocks(
                        request.message_blocks,
                        PostState.RESOLVED,
                        actor=request.actor,
                        at=self._clock(),
                    ),
                )
            await self._trigger_intake()
            return AlertOutcome(
                reply=(
                    "The model path is healthy. The alert is resolved, intake starts the deferred "
                    "tickets now, and the parked runs resume."
                ),
                applied=True,
            )
        return AlertOutcome(refusal=Refusal.NOT_PERMITTED)

    async def _run(self, request: AlertRequest) -> AlertOutcome:
        post = SlackPostRef(request.channel, request.message_ts)
        if request.action == ACKNOWLEDGE:
            await self._acknowledge_post(request, post, self._clock())
            await self._record(request, "acknowledged")
            return AlertOutcome(reply=f"Acknowledged by <@{request.user}>.", applied=True)
        if request.action != RESTART:
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)

        async def _effect() -> dict[str, Any]:
            outcome = await self._restart(request.execution_id, request.actor)
            if not outcome.restarted:
                raise RestartNotTaken(outcome.message)
            return {"message": outcome.message, "actor": request.actor}

        ref = SideEffectRef(
            task_id=f"slack-restart@{self._tenant}",
            execution_id=request.execution_id,
            operation=RESTART_OPERATION,
            target=request.execution_id,
        )
        try:
            outcome = await self._ledger.run_once(ref, _effect)
        except RestartNotTaken as not_taken:
            return AlertOutcome(reply=str(not_taken))
        if outcome.replayed or outcome.in_flight:
            if outcome.replayed and request.message_ts and shows_buttons(request.message_blocks):
                restarter = str((outcome.result or {}).get("actor") or "")
                await self._resolve_run_post(request, post, restarter)
            return AlertOutcome(refusal=Refusal.ALREADY_RESTARTED)
        await self._record(request, "restarted")
        if request.message_ts and request.message_blocks:
            await self._resolve_run_post(request, post, request.actor)
        return AlertOutcome(reply=str((outcome.result or {}).get("message") or ""), applied=True)

    async def _resolve_run_post(
        self, request: AlertRequest, post: SlackPostRef, restarter: str
    ) -> None:
        await self._posts.update(
            post,
            text=f"Restarted by {actor_words(restarter)}" if restarter else "Restarted",
            blocks=status_blocks(
                request.message_blocks, PostState.RESTARTED, actor=restarter, at=self._clock()
            ),
        )

    async def _resolve_clicked_post(self, request: AlertRequest) -> None:
        if not (request.message_ts and shows_buttons(request.message_blocks)):
            return
        closed = await self._episodes.resolved(self._tenant, request.alert_id)
        post = SlackPostRef(request.channel, request.message_ts, admin=request.admin)
        stored: list[dict[str, Any]] | None = None
        if closed is not None and request.admin and request.message_ts == closed.admin_slack_ts:
            post = SlackPostRef(
                request.channel,
                request.message_ts,
                admin=True,
                owner_token=closed.admin_slack_token,
            )
            stored = stored_blocks(closed.admin_slack_blocks)
        elif closed is not None and not request.admin and request.message_ts == closed.slack_ts:
            stored = stored_blocks(closed.slack_blocks)
        blocks = stored or list(request.message_blocks)
        await self._posts.update(
            post, text=RESOLVED_LINE, blocks=status_blocks(blocks, PostState.RESOLVED)
        )

    async def _acknowledge_post(
        self,
        request: AlertRequest,
        post: SlackPostRef,
        at: datetime,
        stored: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not post.ts:
            return []
        blocks: Sequence[dict[str, Any]] = (
            request.message_blocks or stored or title_blocks(model_path_title(self._settings))
        )
        edited = status_blocks(blocks, PostState.ACKNOWLEDGED, actor=request.actor, at=at)
        await self._posts.update(
            post, text=f"Acknowledged by {actor_words(request.actor)}", blocks=edited
        )
        return edited

    async def _record(self, request: AlertRequest, decision: str) -> None:
        execution = request.execution_id or f"{request.subject}:{request.alert_id}"
        await self._audit(
            AuditEvent(
                event_id=f"{execution}:{ALERT_ACTION_EVENT}:{uuid.uuid4().hex}",
                task_id="",
                execution_id=execution[:64],
                event_type=ALERT_ACTION_EVENT,
                workflow_id=request.workflow_id or None,
                decision=decision,
                actor_type=request.actor_type,
                actor_id=request.actor[:128],
                correlation_ids={
                    "tenant": self._tenant,
                    "alert": f"{request.subject}:{request.alert_id}",
                    "action": request.action,
                    "basis": request.basis,
                },
            )
        )


def _post_blocks(episode: ModelPathEpisode, post: SlackPostRef) -> list[dict[str, Any]] | None:
    if post.admin and post.ts == episode.admin_slack_ts:
        return stored_blocks(episode.admin_slack_blocks)
    if not post.admin and post.ts == episode.slack_ts:
        return stored_blocks(episode.slack_blocks)
    return None


def build_tenant_alert_actions(settings: Any, client: Any) -> TenantAlertActions:
    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.orchestration.intake_runtime import build_model_path_gate
    from services.orchestration.limit_grants import (
        BudgetPeriod,
        LimitGrantDecisions,
        LimitRequests,
        PostgresLimitGrants,
    )
    from services.orchestration.limit_parks import (
        LimitParkResumption,
        PostgresLimitParkedRuns,
        TemporalLimitParkResumer,
    )
    from services.orchestration.model_path import PostgresModelPathEpisodes
    from services.orchestration.temporal_activities.persistence import record_audit_event
    from services.orchestration.tenant_jobs import (
        PostgresTenantPauses,
        TenantJobs,
        read_period_budget,
    )

    ledger = PostgresSideEffectLedger()

    async def _restart(execution_id: str, actor: str) -> RestartOutcome:
        from services.orchestration.task_restart import run_restart_task

        issue_key = await _execution_issue_key(execution_id)
        if not issue_key:
            return RestartOutcome(False, f"Execution {execution_id} has no ticket to restart.")
        result = await run_restart_task(issue_key, actor=actor, settings=settings)
        return RestartOutcome(result.restarted, result.message)

    async def _validate() -> str:
        from services.validation.report import render_tenant_report
        from services.validation.tenant import TenantValidationRequest, posture, validate_tenant

        report = await validate_tenant(settings, TenantValidationRequest(), client=client)
        return render_tenant_report(report, posture(settings))

    async def _trigger_intake() -> None:
        from scripts.intake import intake_schedule_id

        from services.integration.tickets import resolve_ticket_backend_name

        schedule = intake_schedule_id(settings, resolve_ticket_backend_name(settings))
        try:
            await client.get_schedule_handle(schedule).trigger()
        except Exception:
            log.warning("intake_schedule_trigger_failed", schedule=schedule, exc_info=True)

    async def _validate_platform() -> str:
        from services.validation.platform import validate_deployment
        from services.validation.report import render_platform_report
        from services.validation.tenant import posture

        report = await validate_deployment(
            settings, tenants=[settings.tenant.strip()], client=client
        )
        return render_platform_report(report, posture(settings))

    async def _period() -> BudgetPeriod | None:
        budget = await read_period_budget(settings, grants=PostgresLimitGrants())
        return budget.period if budget is not None else None

    return TenantAlertActions(
        settings=settings,
        gate=build_model_path_gate(settings, ledger, client=client),
        episodes=PostgresModelPathEpisodes(),
        ledger=ledger,
        audit=record_audit_event,
        posts=SlackPosts(settings),
        restart=_restart,
        validate=_validate,
        trigger_intake=_trigger_intake,
        validate_platform=_validate_platform,
        limits=LimitGrantDecisions(
            store=PostgresLimitGrants(),
            audit=record_audit_event,
            posts=SlackPosts(settings),
            resume=LimitParkResumption(PostgresLimitParkedRuns(), TemporalLimitParkResumer(client)),
        ),
        limit_requests=LimitRequests(
            settings=settings,
            store=PostgresLimitGrants(),
            audit=record_audit_event,
            period=_period,
        ),
        tenant_jobs=TenantJobs(
            settings=settings,
            pauses=PostgresTenantPauses(),
            audit=record_audit_event,
            posts=SlackPosts(settings),
            trigger_intake=_trigger_intake,
        ),
    )


class ForwardedAlertActions:
    """Take an admitted alert click on its tenant's worker, waiting a bound for the outcome.

    A click whose action outlasts the bound keeps running on the worker, which edits the post
    and replies in its thread, and the click is answered that the worker is taking it. When no
    worker has started the action by then, as while the tenant worker restarts, the click is
    answered that it has not been picked up.
    """

    def __init__(
        self,
        client: Any,
        settings: Any,
        *,
        bound_seconds: float = FORWARD_BOUND_SECONDS,
    ) -> None:
        self._client = client
        self._settings = settings
        self._bound = bound_seconds

    async def act(self, request: AlertRequest) -> AlertOutcome:
        from datetime import timedelta

        from services.orchestration.task_queues import tenant_task_queue
        from services.orchestration.temporal_workflows.alert_action import AlertActionWorkflow

        handle = await self._client.start_workflow(
            AlertActionWorkflow.run,
            request,
            id=f"slack-alert-{request.tenant}-{uuid.uuid4().hex[:12]}",
            task_queue=tenant_task_queue(self._settings, request.tenant),
            execution_timeout=timedelta(minutes=FORWARD_TIMEOUT_MINUTES),
        )
        try:
            return await asyncio.wait_for(handle.result(), timeout=self._bound)
        except TimeoutError:
            pass
        from temporalio.api.enums.v1 import PendingActivityState
        from temporalio.client import WorkflowExecutionStatus

        described = await handle.describe()
        if described.status != WorkflowExecutionStatus.RUNNING:
            return await handle.result()
        started = PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
        if any(a.state == started for a in described.raw_description.pending_activities):
            return AlertOutcome(reply=_FORWARDED, applied=True)
        return AlertOutcome(reply=_NOT_STARTED)


async def _execution_issue_key(execution_id: str) -> str:
    from database.engine import shared_platform_engine
    from database.models.schema import executions
    from sqlalchemy import select

    stmt = select(executions.c.task_id).where(executions.c.execution_id == execution_id)
    async with shared_platform_engine().connect() as conn:
        row = (await conn.execute(stmt)).first()
    return str(row.task_id) if row is not None else ""
