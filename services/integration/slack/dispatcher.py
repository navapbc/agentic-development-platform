"""Turn a verified Slack click into one decision on the park its button was posted for.

Both transports call the same dispatcher. The tenant comes from the transport, the request
path on the inbound webhook or the worker's own tenant for Socket Mode, and the token's tenant
must match it before any store or workflow is used. A click of the shared Slack app is decided by
the tenant whose escalation.yaml names the click's channel, and a click from a channel that routes
to no tenant is refused without a dispatcher. The decision reaches the workflow as an
Update whose ID derives from the park, so a double click, a retried click, and a click from
either transport resolve to the first decision. The workflow's validator refuses a decision for
a park it is not waiting on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from services.integration.slack.tokens import (
    ActionToken,
    TokenRefused,
    park_digest,
    verify_action_token,
)
from services.orchestration.escalation import GATE_KIND, SlackSettings

DEFAULT_BOUND_SECONDS = 2.0
SLACK_ACTOR_TYPE = "slack"
REFUSAL_EVENT = "SLACK_ACTION_REFUSED"

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
    NOT_PERMITTED = "not_permitted"
    EARLIER_PARK = "earlier_park"
    NOT_WAITING = "not_waiting"
    ALREADY_DECIDED = "already_decided"
    UNROUTED = "unrouted"


_REPLIES = {
    Refusal.DISABLED: "Slack decisions are not enabled for this tenant.",
    Refusal.UNVERIFIED: "This action could not be verified. Use the Next step command instead.",
    Refusal.EXPIRED: "This action has expired. Use the Next step command instead.",
    Refusal.WORKSPACE: "This action is not accepted from this workspace or channel.",
    Refusal.NOT_ALLOWED: "You are not on the approver list for this decision.",
    Refusal.NOT_PERMITTED: "This action is not available for this run.",
    Refusal.EARLIER_PARK: (
        "This message belongs to an earlier park of this run. Use the most recent message."
    ),
    Refusal.NOT_WAITING: (
        "The run is not waiting for this decision. If it parked moments ago, try again shortly."
    ),
    Refusal.UNROUTED: (
        "This channel is not the escalation channel of a tenant, as after a tenant moves its "
        "alerts to another channel. Use the Next step command in this message instead."
    ),
}
_UNCONFIRMED = "The decision is not yet confirmed. Click again to see the outcome."


@dataclass(frozen=True)
class SlackClick:
    value: str
    action: str
    organization: str
    """The Enterprise Grid ID when the click carries one, else the user's workspace ID."""
    user: str
    channel: str

    @property
    def actor(self) -> str:
        return slack_actor(self.organization, self.user)


def slack_actor(organization: str, user: str) -> str:
    return f"slack:{organization}/{user}"


def click_from_payload(payload: dict[str, Any]) -> SlackClick:
    """Read the click fields from a ``block_actions`` payload.

    An org-wide Grid install sends ``team`` as null, so the organization comes from
    ``enterprise.id`` when present and from ``user.team_id`` otherwise.
    """
    user = payload.get("user") or {}
    enterprise = payload.get("enterprise") or {}
    action = (payload.get("actions") or [{}])[0]
    channel = (payload.get("channel") or {}).get("id")
    return SlackClick(
        value=str(action.get("value") or ""),
        action=str(action.get("action_id") or ""),
        organization=str(enterprise.get("id") or user.get("team_id") or ""),
        user=str(user.get("id") or ""),
        channel=str(channel or (payload.get("container") or {}).get("channel_id") or ""),
    )


@dataclass(frozen=True)
class RecordedDecision:
    decision: str
    actor: str


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


class DecisionRecords(Protocol):
    async def workflow_tenant(self, execution_id: str, workflow_id: str) -> str | None: ...

    async def decision(self, event_id: str) -> RecordedDecision | None: ...

    async def record_refusal(self, refusal: RefusalRecord) -> None: ...


def update_id(tenant: str, park_key: str) -> str:
    return f"slack-decision:{tenant}:{park_digest(park_key)}"


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
    ) -> None:
        self._tenant = tenant
        self._action_key = action_key
        self._slack_settings = slack_settings
        self._decisions = decisions
        self._records = records
        self._bound = bound_seconds

    async def dispatch(
        self, click: SlackClick, *, retained_channel: str | None = None
    ) -> DispatchResult:
        """Decide the click, which must come from the tenant's channel, or from retained_channel,
        a channel the tenant left whose claim it still holds."""
        settings = self._slack_settings()
        if settings is None:
            return await self._refuse(click, Refusal.DISABLED)
        try:
            token = verify_action_token(click.value, self._action_key)
        except TokenRefused as refused:
            reason = Refusal.EXPIRED if refused.reason == "expired" else Refusal.UNVERIFIED
            return await self._refuse(click, reason)
        if token.tenant != self._tenant:
            return await self._refuse(click, Refusal.UNVERIFIED)
        channels = {settings.channel, *([retained_channel] if retained_channel else [])}
        if click.organization != settings.organization_id or click.channel not in channels:
            return await self._refuse(click, Refusal.WORKSPACE, token)
        approvers = settings.allowlist.get(token.kind, ())
        if not any(
            user.organization_id == click.organization and user.user_id == click.user
            for user in approvers
        ):
            return await self._refuse(click, Refusal.NOT_ALLOWED, token)
        if click.action not in token.actions:
            return await self._refuse(click, Refusal.NOT_PERMITTED, token)
        owner = await self._records.workflow_tenant(token.execution_id, token.workflow_id)
        if owner != self._tenant:
            return await self._refuse(click, Refusal.UNVERIFIED)
        return await self._apply(click, token)

    async def _apply(self, click: SlackClick, token: ActionToken) -> DispatchResult:
        update = _UPDATES.get(token.kind, _REPAIR_UPDATE)
        args = [token.park_key, click.action in _AFFIRMATIVE, click.actor, SLACK_ACTOR_TYPE]
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
        self, click: SlackClick, reason: Refusal, token: ActionToken | None = None
    ) -> DispatchResult:
        await self._record(click, reason, token)
        return DispatchResult(reply=_REPLIES[reason], refusal=reason)

    async def _record(self, click: SlackClick, reason: Refusal, token: ActionToken | None) -> None:
        record = RefusalRecord(
            tenant=self._tenant,
            reason=reason,
            organization=click.organization,
            user=click.user,
            execution_id=token.execution_id if token else "",
            workflow_id=token.workflow_id if token else "",
            stage=token.stage if token else "",
            park_digest=park_digest(token.park_key) if token else "",
        )
        await self._records.record_refusal(record)


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


def _outcome_reply(decision: str, actor: str, *, already: bool = False) -> str:
    word = _DECISION_WORDS.get(decision, decision)
    if already:
        return f"This park was already decided. {word} by {actor or 'an operator'}."
    return f"{word} by {actor}."


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
            actor_type=SLACK_ACTOR_TYPE,
            actor_id=slack_actor(refusal.organization, refusal.user)[:128],
            reason=refusal.reason.value,
            correlation_ids=refusal.correlation_ids,
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
