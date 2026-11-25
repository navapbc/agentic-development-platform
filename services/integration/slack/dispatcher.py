"""Turn a verified Slack click into one decision on the park its button was posted for.

Both transports call the same dispatcher. The tenant comes from the transport, the request
path on the inbound webhook or the worker's own tenant for Socket Mode, and the token's tenant
must match it before any store or workflow is used. A click of the shared Slack app is decided by
the tenant whose escalation.yaml names the click's channel, and a click from a channel that routes
to no tenant is refused without a dispatcher. The decision reaches the workflow as an
Update whose ID derives from the park, so a double click, a retried click, and a click from
either transport resolve to the first decision. The Update carries the basis of the approval,
which the workflow records on the decision audit event. The workflow's validator refuses a
decision for a park it is not waiting on.

A click on an alert button, Acknowledge, Run validation, Fix done, or Restart run, passes the
same signature, channel, and approver checks under the ``repair`` kind, then goes to the
dispatcher's alert actions, which refuse a click on a resolved episode, an earlier post, or a run
already restarted, and audit every action they take with the actor and the basis.

A click on an alert button in the platform administrator channel is decided by ``dispatch_admin``
under the administrator allowlist of platform-escalation.yaml in place of the tenant's ``repair``
kind, with its groups read under the administrator group scope. Its refusals and the actions it
takes are audited under the administrator actor type.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from services.integration.slack.messages import (
    SLACK_ACTOR_PREFIX,
    PostState,
    actor_mrkdwn,
    status_blocks,
    title_blocks,
)
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    LIMIT_REQUEST_ALERT,
    ActionToken,
    LimitRequestRef,
    TokenRefusal,
    TokenRefused,
    park_digest,
    verify_action_token,
)
from services.orchestration.escalation import GATE_KIND, Approvers, SlackSettings
from services.orchestration.platform_escalation import ADMIN_GROUP_SCOPE

DEFAULT_BOUND_SECONDS = 2.0
GROUP_STALE_SECONDS = 600
SLACK_ACTOR_TYPE = "slack"
SLACK_ADMIN_ACTOR_TYPE = "slack_admin"
REFUSAL_EVENT = "SLACK_ACTION_REFUSED"
CHANNEL_BASIS = "channel"
USER_BASIS = "user"
GROUP_BASIS_PREFIX = "group:"

_UPDATES = {GATE_KIND: "apply_gate_decision"}
_REPAIR_UPDATE = "apply_repair_decision"
_AFFIRMATIVE = frozenset({"approve", "retry"})
_DECISION_WORDS = {
    "approved": "Approved",
    "rejected": "Rejected",
    "retry": "Retry requested",
    "abandon": "Abandoned",
}


class Refusal(StrEnum):
    DISABLED = "slack_disabled"
    UNVERIFIED = "unverified"
    EXPIRED = "expired"
    WORKSPACE = "workspace"
    NOT_ALLOWED = "not_allowed"
    GROUP_STALE = "group_stale"
    NOT_PERMITTED = "not_permitted"
    EARLIER_PARK = "earlier_park"
    NOT_WAITING = "not_waiting"
    ALREADY_DECIDED = "already_decided"
    UNROUTED = "unrouted"
    RESOLVED = "resolved"
    EARLIER_POST = "earlier_post"
    ALREADY_RESTARTED = "already_restarted"


_REPLIES = {
    Refusal.DISABLED: "Slack decisions are not enabled for this tenant.",
    Refusal.UNVERIFIED: "This action could not be verified, so it was not applied.",
    Refusal.EXPIRED: "This action has expired, so it was not applied.",
    Refusal.WORKSPACE: "This action is not accepted from this workspace or channel.",
    Refusal.NOT_ALLOWED: "You are not on the approver list for this decision.",
    Refusal.GROUP_STALE: (
        "This decision admits you through a Slack user group whose members the platform has not "
        "read from Slack in the last ten minutes. Ask a platform administrator to check the group "
        "refresh."
    ),
    Refusal.NOT_PERMITTED: "This action is not available for this run.",
    Refusal.EARLIER_PARK: (
        "This message belongs to an earlier park of this run. Use the most recent message."
    ),
    Refusal.NOT_WAITING: (
        "The run is not waiting for this decision. If it parked moments ago, try again shortly."
    ),
    Refusal.UNROUTED: (
        "This channel is no longer the tenant's escalation channel. The alert's actions are "
        "available in the tenant's current channel."
    ),
    Refusal.RESOLVED: "This alert is already resolved.",
    Refusal.EARLIER_POST: (
        "This message belongs to an earlier alert of this outage. Use the most recent message."
    ),
    Refusal.ALREADY_RESTARTED: "This run was already restarted from this alert.",
}
_UNCONFIRMED = "The decision is not yet confirmed. Click again to see the outcome."
REFRESHED_REPLY = "These buttons expired. Fresh buttons are posted in this message's thread."


@dataclass(frozen=True)
class SlackClick:
    value: str
    action: str
    organization: str
    """The Enterprise Grid ID when the click carries one, else the user's workspace ID."""
    user: str
    channel: str
    message_ts: str = ""
    message_blocks: tuple[dict[str, Any], ...] = ()
    """The clicked message's blocks, which an edit of the post keeps."""
    message_text: str = ""
    """The clicked message's fallback text, the title an edit renders when it has no blocks."""
    option: str = ""
    """The value of the option a select click chose."""

    @property
    def actor(self) -> str:
        return slack_actor(self.organization, self.user)


def slack_actor(organization: str, user: str) -> str:
    return f"{SLACK_ACTOR_PREFIX}{organization}/{user}"


def click_from_payload(payload: dict[str, Any]) -> SlackClick:
    """Read the click fields from a ``block_actions`` payload.

    An org-wide Grid install sends ``team`` as null, so the organization comes from
    ``enterprise.id`` when present and from ``user.team_id`` otherwise.
    """
    user = payload.get("user") or {}
    enterprise = payload.get("enterprise") or {}
    action = (payload.get("actions") or [{}])[0]
    channel = (payload.get("channel") or {}).get("id")
    message = payload.get("message") or {}
    container = payload.get("container") or {}
    option = action.get("selected_option") or {}
    value = action.get("value") or ""
    if action.get("type") == "static_select":
        value = _paired_token(message, str(action.get("block_id") or ""))
    return SlackClick(
        value=str(value),
        action=str(action.get("action_id") or ""),
        organization=str(enterprise.get("id") or user.get("team_id") or ""),
        user=str(user.get("id") or ""),
        channel=str(channel or container.get("channel_id") or ""),
        message_ts=str(container.get("message_ts") or message.get("ts") or ""),
        message_blocks=tuple(
            block for block in message.get("blocks") or () if isinstance(block, dict)
        ),
        message_text=str(message.get("text") or ""),
        option=str(option.get("value") or "") if isinstance(option, dict) else "",
    )


def _paired_token(message: dict[str, Any], block_id: str) -> str:
    """Return the token of the Acknowledge button beside a select in the clicked message.

    The token must permit the select's action, which the dispatcher checks once it verifies it.
    """
    for block in message.get("blocks") or ():
        if not isinstance(block, dict) or block.get("block_id") != block_id:
            continue
        for element in block.get("elements") or ():
            if isinstance(element, dict) and element.get("action_id") == ACKNOWLEDGE:
                return str(element.get("value") or "")
    return ""


@dataclass(frozen=True)
class RecordedDecision:
    decision: str
    actor: str


@dataclass(frozen=True)
class ApproverGroup:
    members: frozenset[str]
    refreshed_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return now - self.refreshed_at <= timedelta(seconds=GROUP_STALE_SECONDS)


@dataclass(frozen=True)
class RefusalRecord:
    tenant: str
    reason: Refusal
    organization: str
    user: str
    execution_id: str = ""
    workflow_id: str = ""
    stage: str = ""
    park_digest: str = ""
    route: str = ""
    channel: str = ""
    admin: bool = False

    @property
    def actor_type(self) -> str:
        return SLACK_ADMIN_ACTOR_TYPE if self.admin else SLACK_ACTOR_TYPE

    @property
    def correlation_ids(self) -> dict[str, str]:
        ids = {"tenant": self.tenant, "park_digest": self.park_digest}
        if self.route:
            ids |= {"route": self.route, "channel": self.channel}
        return ids


@dataclass(frozen=True)
class DispatchResult:
    reply: str
    refusal: Refusal | None = None
    decision: str = ""
    actor: str = ""
    applied: bool = False
    pending: bool = False
    alert: bool = False
    """An alert action's outcome, which a reply carries without replacing the alert post."""
    post: tuple[dict[str, Any], ...] = ()
    """The blocks that replace the clicked post after an accepted decision."""

    @property
    def accepted(self) -> bool:
        return self.refusal is None and not self.pending


class DecisionRejected(Exception):
    """The workflow refused the decision because it is not waiting on the token's park."""


class DecisionPending(Exception):
    """The workflow did not answer the decision Update within the dispatcher's bound."""


class WorkflowDecisions(Protocol):
    async def apply(
        self, workflow_id: str, update: str, args: list[Any], update_id: str
    ) -> Any: ...

    async def waiting_park(self, workflow_id: str) -> str: ...

    async def refresh(self, workflow_id: str, park_key: str, update_id: str) -> Any:
        """Ask the run to post fresh buttons for ``park_key``, or raise DecisionRejected."""
        ...


class DecisionRecords(Protocol):
    async def workflow_tenant(self, execution_id: str, workflow_id: str) -> str | None: ...

    async def decision(self, event_id: str) -> RecordedDecision | None: ...

    async def record_refusal(self, refusal: RefusalRecord) -> None: ...

    async def approver_groups(
        self, tenant: str, group_ids: Sequence[str]
    ) -> Mapping[str, ApproverGroup]: ...


@dataclass(frozen=True)
class AlertRequest:
    tenant: str
    subject: str
    alert_id: str
    action: str
    actor: str
    user: str
    basis: str
    channel: str
    message_ts: str
    message_blocks: tuple[dict[str, Any], ...]
    execution_id: str = ""
    workflow_id: str = ""
    admin: bool = False
    """Admitted under the administrator allowlist from the administrator channel."""
    option: str = ""
    """The option a select click chose, such as the amount of a Request increase."""

    @property
    def actor_type(self) -> str:
        return SLACK_ADMIN_ACTOR_TYPE if self.admin else SLACK_ACTOR_TYPE


@dataclass(frozen=True)
class AlertOutcome:
    reply: str = ""
    refusal: Refusal | None = None
    applied: bool = False


class AlertActions(Protocol):
    async def act(self, request: AlertRequest) -> AlertOutcome: ...


def update_id(tenant: str, park_key: str) -> str:
    return f"slack-decision:{tenant}:{park_digest(park_key)}"


def refresh_id(tenant: str, park_key: str, *, now: float | None = None) -> str:
    """Name a refresh Update once per minute, so Temporal answers repeated clicks with one."""
    minute = int((time.time() if now is None else now) // 60)
    return f"slack-refresh:{tenant}:{park_digest(park_key)}:{minute}"


class SlackDispatcher:
    def __init__(
        self,
        *,
        tenant: str,
        action_key: bytes,
        slack_settings: Callable[[], SlackSettings | None],
        decisions: WorkflowDecisions,
        records: DecisionRecords,
        bound_seconds: float = DEFAULT_BOUND_SECONDS,
        alert_actions: AlertActions | None = None,
    ) -> None:
        self._tenant = tenant
        self._action_key = action_key
        self._slack_settings = slack_settings
        self._decisions = decisions
        self._records = records
        self._bound = bound_seconds
        self._alert_actions = alert_actions

    async def dispatch(
        self, click: SlackClick, *, retained_channel: str | None = None
    ) -> DispatchResult:
        """Decide the click, which must come from the tenant's channel, or from retained_channel,
        a channel the tenant left whose claim it still holds."""
        settings = self._slack_settings()
        if settings is None:
            return await self._refuse(click, Refusal.DISABLED)
        channels = {settings.channel, *([retained_channel] if retained_channel else [])}
        try:
            token = verify_action_token(click.value, self._action_key)
        except TokenRefused as refused:
            if refused.reason != TokenRefusal.EXPIRED:
                return await self._refuse(click, Refusal.UNVERIFIED)
            return await self._expired(click, refused.token, settings, channels)
        if token.tenant != self._tenant:
            return await self._refuse(click, Refusal.UNVERIFIED)
        if click.organization != settings.organization_id or click.channel not in channels:
            return await self._refuse(click, Refusal.WORKSPACE, token)
        basis = await self._approval_basis(settings.approvers(token.kind), click)
        if isinstance(basis, Refusal):
            return await self._refuse(click, basis, token)
        if click.action not in token.actions:
            return await self._refuse(click, Refusal.NOT_PERMITTED, token)
        if token.alert:
            return await self._apply_alert(click, token, basis)
        owner = await self._records.workflow_tenant(token.execution_id, token.workflow_id)
        if owner != self._tenant:
            return await self._refuse(click, Refusal.UNVERIFIED)
        return await self._apply(click, token, basis)

    async def _expired(
        self,
        click: SlackClick,
        token: ActionToken | None,
        settings: SlackSettings,
        channels: set[str],
    ) -> DispatchResult:
        park_key = _refreshed_park(token)
        admitted = (
            token is not None
            and bool(park_key)
            and token.tenant == self._tenant
            and click.organization == settings.organization_id
            and click.channel in channels
            and click.action in token.actions
            and not isinstance(
                await self._approval_basis(settings.approvers(token.kind), click), Refusal
            )
            and await self._records.workflow_tenant(token.execution_id, token.workflow_id)
            == self._tenant
        )
        if not admitted or token is None:
            return await self._refuse(click, Refusal.EXPIRED)
        try:
            await asyncio.wait_for(
                self._decisions.refresh(
                    token.workflow_id, park_key, refresh_id(self._tenant, park_key)
                ),
                timeout=self._bound,
            )
        except TimeoutError, DecisionPending:
            return await self._refuse(click, Refusal.EXPIRED, token)
        except DecisionRejected:
            if token.alert:
                return await self._refuse(click, Refusal.NOT_WAITING, token)
            return await self._rejected(click, token)
        await self._record(click, Refusal.EXPIRED, token)
        return DispatchResult(reply=REFRESHED_REPLY, refusal=Refusal.EXPIRED)

    async def dispatch_admin(
        self, click: SlackClick, *, channel: str, organization: str, approvers: Approvers
    ) -> DispatchResult:
        """Decide an alert click from the administrator ``channel`` under the administrator
        ``approvers``, whose groups are read under ADMIN_GROUP_SCOPE."""
        try:
            token = verify_action_token(click.value, self._action_key)
        except TokenRefused as refused:
            reason = (
                Refusal.EXPIRED if refused.reason == TokenRefusal.EXPIRED else Refusal.UNVERIFIED
            )
            return await self._refuse(click, reason, admin=True)
        if token.tenant != self._tenant:
            return await self._refuse(click, Refusal.UNVERIFIED, admin=True)
        if click.organization != organization or click.channel != channel:
            return await self._refuse(click, Refusal.WORKSPACE, token, admin=True)
        basis = await self._approval_basis(approvers, click, group_scope=ADMIN_GROUP_SCOPE)
        if isinstance(basis, Refusal):
            return await self._refuse(click, basis, token, admin=True)
        if not token.alert or click.action not in token.actions:
            return await self._refuse(click, Refusal.NOT_PERMITTED, token, admin=True)
        return await self._apply_alert(click, token, basis, admin=True)

    async def _approval_basis(
        self, approvers: Approvers, click: SlackClick, *, group_scope: str = ""
    ) -> str | Refusal:
        if approvers.anyone_in_channel:
            return CHANNEL_BASIS
        if approvers.names_user(click.organization, click.user):
            return USER_BASIS
        group_ids = approvers.group_ids(click.organization)
        if not group_ids:
            return Refusal.NOT_ALLOWED
        records = await self._records.approver_groups(group_scope or self._tenant, group_ids)
        now = datetime.now(UTC)
        stale = False
        for group_id in group_ids:
            record = records.get(group_id)
            if record is None or click.user not in record.members:
                continue
            if record.is_fresh(now):
                return f"{GROUP_BASIS_PREFIX}{group_id}"
            stale = True
        return Refusal.GROUP_STALE if stale else Refusal.NOT_ALLOWED

    async def _apply(self, click: SlackClick, token: ActionToken, basis: str) -> DispatchResult:
        update = _UPDATES.get(token.kind, _REPAIR_UPDATE)
        args = [
            token.park_key,
            click.action in _AFFIRMATIVE,
            click.actor,
            SLACK_ACTOR_TYPE,
            basis,
        ]
        try:
            outcome = await asyncio.wait_for(
                self._decisions.apply(
                    token.workflow_id, update, args, update_id(self._tenant, token.park_key)
                ),
                timeout=self._bound,
            )
        except TimeoutError, DecisionPending:
            return DispatchResult(reply=_UNCONFIRMED, pending=True)
        except DecisionRejected:
            return await self._rejected(click, token)
        decision, actor = str(outcome.decision), str(outcome.actor)
        return DispatchResult(
            reply=_outcome_reply(decision, actor),
            decision=decision,
            actor=actor,
            applied=bool(outcome.applied),
            post=_decided_post(click, decision, actor),
        )

    async def _apply_alert(
        self, click: SlackClick, token: ActionToken, basis: str, *, admin: bool = False
    ) -> DispatchResult:
        if self._alert_actions is None:
            return await self._refuse(click, Refusal.NOT_PERMITTED, token, admin=admin)
        if token.execution_id:
            owner = await self._records.workflow_tenant(token.execution_id, token.workflow_id)
            if owner != self._tenant:
                return await self._refuse(click, Refusal.UNVERIFIED, admin=admin)
        outcome = await self._alert_actions.act(
            AlertRequest(
                tenant=self._tenant,
                subject=token.alert_subject,
                alert_id=token.alert_id,
                action=click.action,
                actor=click.actor,
                user=click.user,
                basis=basis,
                channel=click.channel,
                message_ts=click.message_ts,
                message_blocks=click.message_blocks,
                execution_id=token.execution_id,
                workflow_id=token.workflow_id,
                admin=admin,
                option=click.option,
            )
        )
        if outcome.refusal is not None:
            return await self._refuse(click, outcome.refusal, token, admin=admin)
        return DispatchResult(
            reply=outcome.reply,
            decision=click.action,
            actor=click.actor,
            applied=outcome.applied,
            alert=True,
        )

    async def _rejected(self, click: SlackClick, token: ActionToken) -> DispatchResult:
        recorded = await self._records.decision(token.decision_event_id)
        if recorded is not None:
            await self._record(click, Refusal.ALREADY_DECIDED, token)
            return DispatchResult(
                reply=_outcome_reply(recorded.decision, recorded.actor, already=True),
                refusal=Refusal.ALREADY_DECIDED,
                decision=recorded.decision,
                actor=recorded.actor,
            )
        waiting = await self._decisions.waiting_park(token.workflow_id)
        reason = (
            Refusal.EARLIER_PARK if waiting and waiting != token.park_key else Refusal.NOT_WAITING
        )
        return await self._refuse(click, reason, token)

    async def _refuse(
        self,
        click: SlackClick,
        reason: Refusal,
        token: ActionToken | None = None,
        *,
        admin: bool = False,
    ) -> DispatchResult:
        await self._record(click, reason, token, admin=admin)
        return DispatchResult(reply=_REPLIES[reason], refusal=reason)

    async def _record(
        self,
        click: SlackClick,
        reason: Refusal,
        token: ActionToken | None,
        *,
        admin: bool = False,
    ) -> None:
        record = RefusalRecord(
            tenant=self._tenant,
            reason=reason,
            organization=click.organization,
            user=click.user,
            execution_id=token.execution_id if token else "",
            workflow_id=token.workflow_id if token else "",
            stage=token.stage if token else "",
            park_digest=park_digest(token.park_key or token.alert) if token else "",
            admin=admin,
        )
        await self._records.record_refusal(record)


def _refreshed_park(token: ActionToken | None) -> str:
    if token is None:
        return ""
    if not token.alert:
        return token.park_key
    if token.alert_subject != LIMIT_REQUEST_ALERT:
        return ""
    ref = LimitRequestRef.parse(token.alert_id)
    if ref is None or not ref.park_key.startswith(f"{token.execution_id}:"):
        return ""
    return ref.park_key


async def refuse_unrouted(
    click: SlackClick, records: DecisionRecords, *, route: str
) -> DispatchResult:
    """Refuse a click whose channel routes to no tenant, and record the route and the channel."""
    await records.record_refusal(
        RefusalRecord(
            tenant="",
            reason=Refusal.UNROUTED,
            organization=click.organization,
            user=click.user,
            route=route,
            channel=click.channel,
        )
    )
    return DispatchResult(reply=_REPLIES[Refusal.UNROUTED], refusal=Refusal.UNROUTED)


def _decided_post(click: SlackClick, decision: str, actor: str) -> tuple[dict[str, Any], ...]:
    try:
        state = PostState(decision)
    except ValueError:
        return ()
    original = click.message_blocks or (
        title_blocks(click.message_text) if click.message_text else []
    )
    return tuple(status_blocks(original, state, actor=actor, at=datetime.now(UTC)))


def _outcome_reply(decision: str, actor: str, *, already: bool = False) -> str:
    word = _DECISION_WORDS.get(decision, decision)
    who = actor_mrkdwn(actor or ("an operator" if already else ""))
    if already:
        return f"This park was already decided. {word} by {who}."
    return f"{word} by {who}."


class TemporalWorkflowDecisions:
    """Apply decision Updates through a Temporal client the process already holds."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def apply(self, workflow_id: str, update: str, args: list[Any], update_id: str) -> Any:
        from temporalio.client import (
            WorkflowUpdateFailedError,
            WorkflowUpdateRPCTimeoutOrCancelledError,
        )
        from temporalio.service import RPCError, RPCStatusCode

        from services.orchestration.temporal_workflows.draft_pull_request import (
            ParkDecisionOutcome,
        )

        handle = self._client.get_workflow_handle(workflow_id)
        try:
            return await handle.execute_update(
                update, args=args, id=update_id, result_type=ParkDecisionOutcome
            )
        except WorkflowUpdateFailedError as exc:
            raise DecisionRejected(str(exc)) from exc
        except WorkflowUpdateRPCTimeoutOrCancelledError as exc:
            # temporalio raises this in place of the CancelledError that asyncio.wait_for turns
            # into TimeoutError, so the bound would otherwise escape the dispatcher.
            raise DecisionPending from exc
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            raise DecisionRejected(str(exc)) from exc

    async def refresh(self, workflow_id: str, park_key: str, update_id: str) -> Any:
        from temporalio.client import (
            WorkflowUpdateFailedError,
            WorkflowUpdateRPCTimeoutOrCancelledError,
        )
        from temporalio.service import RPCError, RPCStatusCode

        from services.orchestration.suspension import REFRESH_PARK_BUTTONS_UPDATE

        handle = self._client.get_workflow_handle(workflow_id)
        try:
            return await handle.execute_update(
                REFRESH_PARK_BUTTONS_UPDATE, args=[park_key], id=update_id, result_type=bool
            )
        except WorkflowUpdateFailedError as exc:
            raise DecisionRejected(str(exc)) from exc
        except WorkflowUpdateRPCTimeoutOrCancelledError as exc:
            raise DecisionPending from exc
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            raise DecisionRejected(str(exc)) from exc

    async def waiting_park(self, workflow_id: str) -> str:
        try:
            state = await self._client.get_workflow_handle(workflow_id).query("suspension_state")
        except Exception:
            return ""
        return str((state or {}).get("park_key") or "")


class PostgresDecisionRecords:
    """Read run ownership and decisions, and write refusals, through the platform database."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def workflow_tenant(self, execution_id: str, workflow_id: str) -> str | None:
        from database.models.schema import executions
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(executions.c.tenant, executions.c.workflow_id).where(
                        executions.c.execution_id == execution_id
                    )
                )
            ).first()
        if row is None or row.workflow_id != workflow_id:
            return None
        return str(row.tenant or "")

    async def decision(self, event_id: str) -> RecordedDecision | None:
        from database.models.schema import audit_events
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        audit_events.c.decision, audit_events.c.actor_id, audit_events.c.reason
                    ).where(audit_events.c.event_id == event_id)
                )
            ).first()
        if row is None:
            return None
        return RecordedDecision(decision=row.decision or "", actor=row.actor_id or row.reason or "")

    async def record_refusal(self, refusal: RefusalRecord) -> None:
        from database.models.schema import audit_events
        from sqlalchemy.dialects.postgresql import insert

        execution = refusal.execution_id or "unverified"
        stmt = insert(audit_events).values(
            event_id=f"{execution}:{REFUSAL_EVENT}:{uuid.uuid4().hex}",
            task_id="",
            execution_id=execution,
            workflow_id=refusal.workflow_id or None,
            event_type=REFUSAL_EVENT,
            stage=refusal.stage or None,
            actor_type=refusal.actor_type,
            actor_id=slack_actor(refusal.organization, refusal.user)[:128],
            reason=refusal.reason.value,
            correlation_ids=refusal.correlation_ids,
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def approver_groups(
        self, tenant: str, group_ids: Sequence[str]
    ) -> Mapping[str, ApproverGroup]:
        from database.models.schema import slack_approver_groups as groups
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(groups.c.group_id, groups.c.members, groups.c.refreshed_at).where(
                        groups.c.tenant == tenant, groups.c.group_id.in_(list(group_ids))
                    )
                )
            ).all()
        return {
            str(row.group_id): ApproverGroup(
                members=frozenset(str(member) for member in row.members or ()),
                refreshed_at=row.refreshed_at,
            )
            for row in rows
        }
