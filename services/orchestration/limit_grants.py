"""Limit increase grants, stored in Postgres and added to a configured limit when it is checked.

A tenant asks for more of one limit. A run limit is granted to one execution and ends with it. The
tenant model budget is granted for the current LiteLLM budget period and expires at its reset. A
request outside the platform band is refused, and its reply states that the configured limit
must change. A request inside the band reaches the administrators with Approve and Deny, and the
administrator's decision is recorded, audited, and shown on the alert post.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog

from services.integration.slack.dispatcher import AlertOutcome, AlertRequest, Refusal
from services.integration.slack.messages import (
    LIMIT_ACTIONS,
    PostState,
    actor_words,
    alert_buttons,
    labeled_blocks,
    status_blocks,
)
from services.integration.slack.tokens import (
    DENY_LIMIT,
    GRANT_LIMIT,
    LIMIT_ALERT,
    LIMIT_REQUEST_ALERT,
    REQUEST_LIMIT,
    LimitRequestRef,
    alert_ref,
)
from services.orchestration.notifications import (
    AlertMessage,
    SlackPostRef,
    alert_context,
    deliver_admin_alert,
    stored_blocks,
)
from services.orchestration.temporal_activities.persistence import AuditEvent

log = structlog.get_logger("limit_grants")

RUN_SCOPE = "run"
PERIOD_SCOPE = "period"
REQUESTED = "requested"
GRANTED = "granted"
DENIED = "denied"

RUN_LIMITS = {
    "IMPLEMENTATION_MAX_COST_USD": "implementation_max_cost_usd",
    "IMPLEMENTATION_MAX_TOKENS": "implementation_max_tokens",
    "IMPLEMENTATION_MAX_LLM_CALLS": "implementation_max_llm_calls",
    "RUN_MODEL_BUDGET_USD": "run_model_budget_usd",
}
PERIOD_LIMITS = {"TENANT_MODEL_BUDGET_USD": "tenant_model_budget_usd"}
LIMIT_SETTINGS = {**RUN_LIMITS, **PERIOD_LIMITS}

LIMIT_INCREASE_REQUESTED = "LIMIT_INCREASE_REQUESTED"
LIMIT_INCREASE_GRANTED = "LIMIT_INCREASE_GRANTED"
LIMIT_INCREASE_DENIED = "LIMIT_INCREASE_DENIED"
LIMIT_ALERT_TITLE = "Limit increase requested"

_DECISIONS = {GRANT_LIMIT: GRANTED, DENY_LIMIT: DENIED}
_DECISION_EVENTS = {GRANTED: LIMIT_INCREASE_GRANTED, DENIED: LIMIT_INCREASE_DENIED}
_DECISION_STATES = {GRANTED: PostState.GRANTED, DENIED: PostState.DENIED}
_DENIED_KEEPS = frozenset({PostState.ABANDONED.value})
"""A denied request leaves the run parked, so the tenant post keeps its Abandon button."""
_OPTION_FRACTIONS = (0.10,)
"""The fractions of the configured limit Request increase offers below the step band."""
_DURATION = re.compile(r"^(\d+)(mo|s|m|h|d|w)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass(frozen=True)
class BudgetPeriod:
    start: datetime
    reset: datetime


@dataclass(frozen=True)
class LimitGrant:
    request_id: str
    tenant: str
    limit_name: str
    scope: str
    amount: float
    state: str
    requester: str
    execution_id: str = ""
    period_start: datetime | None = None
    approver: str = ""
    created_at: datetime | None = None
    decided_at: datetime | None = None
    expires_at: datetime | None = None
    admin_slack_ts: str = ""
    admin_slack_token: str = ""
    admin_slack_blocks: str = ""
    tenant_slack_channel: str = ""
    tenant_slack_ts: str = ""
    tenant_slack_blocks: str = ""

    def active(self, *, execution_id: str, now: datetime) -> bool:
        """Whether this grant raises the limit of ``execution_id`` at ``now``."""
        if self.state != GRANTED:
            return False
        if self.scope == RUN_SCOPE:
            return bool(execution_id) and self.execution_id == execution_id
        return self.expires_at is not None and now < self.expires_at


class LimitGrants(Protocol):
    async def create(self, grant: LimitGrant) -> None: ...

    async def get(self, tenant: str, request_id: str) -> LimitGrant | None: ...

    async def decide(
        self, tenant: str, request_id: str, *, state: str, approver: str, at: datetime
    ) -> LimitGrant | None:
        """Move a requested grant to ``state`` and return it, or None when it is not requested."""
        ...

    async def record_post(self, tenant: str, request_id: str, post: SlackPostRef) -> None: ...

    async def for_limit(self, tenant: str, limit_name: str) -> list[LimitGrant]: ...


class PostgresLimitGrants:
    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def _get_engine(self) -> Any:
        if self._engine is None:
            from database.engine import shared_platform_engine

            self._engine = shared_platform_engine()
        return self._engine

    async def create(self, grant: LimitGrant) -> None:
        from database.models.schema import limit_grants
        from sqlalchemy import insert

        values = {
            "request_id": grant.request_id,
            "tenant": grant.tenant,
            "limit_name": grant.limit_name,
            "scope": grant.scope,
            "execution_id": grant.execution_id,
            "period_start": grant.period_start,
            "amount": Decimal(str(grant.amount)),
            "state": grant.state,
            "requester": grant.requester[:128],
            "expires_at": grant.expires_at,
            "tenant_slack_channel": grant.tenant_slack_channel,
            "tenant_slack_ts": grant.tenant_slack_ts,
            "tenant_slack_blocks": grant.tenant_slack_blocks,
        }
        async with self._get_engine().begin() as conn:
            await conn.execute(insert(limit_grants).values(**values))

    async def get(self, tenant: str, request_id: str) -> LimitGrant | None:
        from database.models.schema import limit_grants
        from sqlalchemy import select

        stmt = select(limit_grants).where(
            limit_grants.c.tenant == tenant, limit_grants.c.request_id == request_id
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        return _grant(row) if row is not None else None

    async def decide(
        self, tenant: str, request_id: str, *, state: str, approver: str, at: datetime
    ) -> LimitGrant | None:
        from database.models.schema import limit_grants
        from sqlalchemy import update

        stmt = (
            update(limit_grants)
            .where(
                limit_grants.c.tenant == tenant,
                limit_grants.c.request_id == request_id,
                limit_grants.c.state == REQUESTED,
            )
            .values(state=state, approver=approver[:128], decided_at=at)
            .returning(*limit_grants.c)
        )
        async with self._get_engine().begin() as conn:
            row = (await conn.execute(stmt)).first()
        return _grant(row) if row is not None else None

    async def record_post(self, tenant: str, request_id: str, post: SlackPostRef) -> None:
        from database.models.schema import limit_grants
        from sqlalchemy import update

        stmt = (
            update(limit_grants)
            .where(limit_grants.c.tenant == tenant, limit_grants.c.request_id == request_id)
            .values(
                admin_slack_ts=post.ts,
                admin_slack_token=post.owner_token,
                admin_slack_blocks=post.blocks,
            )
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)

    async def for_limit(self, tenant: str, limit_name: str) -> list[LimitGrant]:
        from database.models.schema import limit_grants
        from sqlalchemy import select

        stmt = select(limit_grants).where(
            limit_grants.c.tenant == tenant,
            limit_grants.c.limit_name == limit_name,
            limit_grants.c.state != DENIED,
        )
        async with self._get_engine().connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_grant(row) for row in rows]


def _grant(row: Any) -> LimitGrant:
    return LimitGrant(
        request_id=row.request_id,
        tenant=row.tenant,
        limit_name=row.limit_name,
        scope=row.scope,
        amount=float(row.amount),
        state=row.state,
        requester=row.requester,
        execution_id=row.execution_id,
        period_start=row.period_start,
        approver=row.approver,
        created_at=row.created_at,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
        admin_slack_ts=row.admin_slack_ts,
        admin_slack_token=row.admin_slack_token,
        admin_slack_blocks=row.admin_slack_blocks,
        tenant_slack_channel=row.tenant_slack_channel,
        tenant_slack_ts=row.tenant_slack_ts,
        tenant_slack_blocks=row.tenant_slack_blocks,
    )


def limit_scope(limit_name: str) -> str:
    if limit_name in RUN_LIMITS:
        return RUN_SCOPE
    if limit_name in PERIOD_LIMITS:
        return PERIOD_SCOPE
    raise ValueError(f"{limit_name} is not a limit that can be raised by a grant")


def configured_limit(settings: Any, limit_name: str) -> float:
    return float(getattr(settings, LIMIT_SETTINGS[limit_name]))


def effective_limit(
    configured: float, grants: Sequence[LimitGrant], *, execution_id: str = "", now: datetime
) -> float:
    """Return ``configured`` plus the grants active for ``execution_id`` at ``now``.

    A limit configured as zero sets no limit, and a grant does not turn it into one.
    """
    if configured <= 0:
        return configured
    return configured + sum(
        grant.amount for grant in grants if grant.active(execution_id=execution_id, now=now)
    )


async def effective_value(
    store: LimitGrants,
    tenant: str,
    limit_name: str,
    configured: float,
    *,
    execution_id: str = "",
    now: datetime | None = None,
) -> float:
    limit_scope(limit_name)
    grants = await store.for_limit(tenant, limit_name)
    return effective_limit(
        configured, grants, execution_id=execution_id, now=now or datetime.now(UTC)
    )


def _subtract_months(moment: datetime, months: int) -> datetime:
    year, month = divmod(moment.year * 12 + moment.month - 1 - months, 12)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def tenant_budget_period(team: dict[str, Any] | None, duration: str) -> BudgetPeriod | None:
    """Return the tenant team's current budget period from its LiteLLM team record.

    LiteLLM resets a team's spend at ``budget_reset_at`` and moves the reset one
    TENANT_MODEL_BUDGET_DURATION later, so the period started one duration before the reset.
    """
    reset_text = str((team or {}).get("budget_reset_at") or "")
    matched = _DURATION.match(duration.strip())
    if not reset_text or matched is None:
        return None
    reset = datetime.fromisoformat(reset_text)
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=UTC)
    count, unit = int(matched.group(1)), matched.group(2)
    if unit == "mo":
        return BudgetPeriod(start=_subtract_months(reset, count), reset=reset)
    return BudgetPeriod(start=reset - timedelta(seconds=count * _UNIT_SECONDS[unit]), reset=reset)


@dataclass(frozen=True)
class Band:
    step_percent: float
    period_percent: float

    @classmethod
    def from_settings(cls, settings: Any) -> Band:
        return cls(
            step_percent=float(settings.limit_grant_step_percent),
            period_percent=float(settings.limit_grant_period_percent),
        )

    def step(self, configured: float) -> float:
        return configured * self.step_percent / 100

    def period(self, configured: float) -> float:
        return configured * self.period_percent / 100

    def describe(self, configured: float) -> str:
        return (
            f"{_amount(self.step(configured))} per request, "
            f"{_amount(self.period(configured))} per period"
        )

    def options(
        self, configured: float, already_granted: float, quantum: float
    ) -> tuple[float, ...]:
        """Return the amounts Request increase offers, each a whole ``quantum`` inside the band.

        The offer is a tenth of the configured limit, the step band, and the remaining period
        band when it is smaller than the step band.
        """
        if configured <= 0:
            return ()
        ceiling = min(self.step(configured), self.period(configured) - already_granted)
        candidates = {configured * fraction for fraction in _OPTION_FRACTIONS} | {ceiling}
        amounts = {round(math.floor(c / quantum) * quantum, 2) for c in candidates if c > 0}
        return tuple(sorted(amount for amount in amounts if 0 < amount <= ceiling))

    def refusal(self, configured: float, amount: float, already_granted: float) -> str:
        """Return why ``amount`` falls outside the band, or an empty string inside it."""
        if amount > self.step(configured):
            return (
                f"The request of {_amount(amount)} exceeds the step band of "
                f"{_amount(self.step(configured))}, {self.step_percent:g} percent of the "
                "configured limit."
            )
        if already_granted + amount > self.period(configured):
            return (
                f"The request of {_amount(amount)} with {_amount(already_granted)} already "
                f"granted exceeds the period band of {_amount(self.period(configured))}, "
                f"{self.period_percent:g} percent of the configured limit."
            )
        return ""


def _amount(value: float) -> str:
    return f"{value:g}"


def _quantum(limit_name: str) -> float:
    return 0.01 if limit_name.endswith("_USD") else 1.0


def _granted_to_run(grants: Sequence[LimitGrant], execution_id: str) -> float:
    return sum(
        grant.amount
        for grant in grants
        if grant.state == GRANTED
        and grant.scope == RUN_SCOPE
        and grant.execution_id == execution_id
    )


def period_request_key(tenant: str, period: BudgetPeriod) -> str:
    """Return the key a Request increase on the period budget signs, which names the period."""
    return f"period:{tenant}:TENANT_MODEL_BUDGET_USD:{int(period.start.timestamp())}"


def _granted_to_period(grants: Sequence[LimitGrant], period: BudgetPeriod) -> float:
    return sum(
        grant.amount
        for grant in grants
        if grant.state == GRANTED
        and grant.scope == PERIOD_SCOPE
        and grant.period_start == period.start
    )


async def request_amounts(
    settings: Any,
    store: LimitGrants,
    limit_name: str,
    *,
    execution_id: str,
    period: BudgetPeriod | None = None,
) -> tuple[float, ...]:
    """Return the amounts of ``limit_name`` a Request increase offers.

    A run limit offers the band left to ``execution_id``. The tenant model budget offers the band
    left to ``period``, and nothing without one.
    """
    scope = limit_scope(limit_name)
    if scope == PERIOD_SCOPE and period is None:
        return ()
    grants = await store.for_limit(settings.tenant.strip(), limit_name)
    granted = (
        _granted_to_run(grants, execution_id)
        if scope == RUN_SCOPE
        else _granted_to_period(grants, period)  # type: ignore[arg-type]
    )
    return Band.from_settings(settings).options(
        configured_limit(settings, limit_name), granted, _quantum(limit_name)
    )


@dataclass(frozen=True)
class LimitRequestOutcome:
    accepted: bool
    reply: str
    request_id: str = ""


@dataclass(frozen=True)
class LimitIncreaseRequest:
    """A tenant's request for ``amount`` more of ``limit_name``.

    A run limit names the ``execution_id`` it raises. The tenant model budget names the
    current budget ``period``.
    """

    tenant: str
    limit_name: str
    amount: float
    requester: str
    usage: float
    execution_id: str = ""
    period: BudgetPeriod | None = None
    requester_type: str = "slack"
    tenant_post: SlackPostRef | None = None
    """The tenant post the request was made on, which the decision edits and replies to."""


async def request_limit_increase(
    request: LimitIncreaseRequest,
    *,
    settings: Any,
    store: LimitGrants,
    audit: Callable[[AuditEvent], Awaitable[Any]],
    deliver: Callable[[AlertMessage, Any], list[str]] = deliver_admin_alert,
    now: datetime | None = None,
) -> LimitRequestOutcome:
    """Record a limit increase request and alert the administrators, or refuse it.

    Every request is audited as LIMIT_INCREASE_REQUESTED, with the decision ``requested`` when
    it reached the administrators and ``refused`` when it did not.
    """
    at = now or datetime.now(UTC)
    scope = limit_scope(request.limit_name)
    if scope == RUN_SCOPE and not request.execution_id:
        raise ValueError(f"{request.limit_name} is a run limit and needs the run's execution ID")
    if scope == PERIOD_SCOPE and request.period is None:
        raise ValueError(f"{request.limit_name} is a period limit and needs the budget period")
    configured = configured_limit(settings, request.limit_name)
    band = Band.from_settings(settings)
    grants = await store.for_limit(request.tenant, request.limit_name)
    effective = effective_limit(configured, grants, execution_id=request.execution_id, now=at)
    granted = sum(
        grant.amount
        for grant in grants
        if grant.state == GRANTED and _same_scope(grant, request, scope)
    )
    if request.amount <= 0:
        refusal = "The requested amount must be greater than zero."
    elif configured <= 0:
        refusal = f"{request.limit_name} sets no limit, so there is no limit to raise."
    else:
        refusal = band.refusal(configured, request.amount, granted)
    if refusal:
        reply = f"{refusal} The configured limit {request.limit_name} must change."
        await _audit_request(audit, request, scope, "", "refused", reply)
        return LimitRequestOutcome(accepted=False, reply=reply)

    grant = LimitGrant(
        request_id=uuid.uuid4().hex,
        tenant=request.tenant,
        limit_name=request.limit_name,
        scope=scope,
        amount=request.amount,
        state=REQUESTED,
        requester=request.requester,
        execution_id=request.execution_id if scope == RUN_SCOPE else "",
        period_start=request.period.start if request.period else None,
        created_at=at,
        expires_at=request.period.reset if request.period else None,
        tenant_slack_channel=request.tenant_post.channel if request.tenant_post else "",
        tenant_slack_ts=request.tenant_post.ts if request.tenant_post else "",
        tenant_slack_blocks=request.tenant_post.blocks if request.tenant_post else "",
    )
    await store.create(grant)
    await _audit_request(audit, request, scope, grant.request_id, REQUESTED, "")
    message = limit_alert_message(
        settings, grant, configured=configured, effective=effective, usage=request.usage, band=band
    )
    delivered = await asyncio.to_thread(deliver, message, settings)
    for post in message.posts:
        if post.admin:
            await store.record_post(request.tenant, grant.request_id, post)
            break
    reply = (
        f"The request for {_amount(request.amount)} more of {request.limit_name} is with the "
        "administrators."
        if delivered
        else f"The request for {_amount(request.amount)} more of {request.limit_name} is "
        "recorded, but no administrator destination received it."
    )
    return LimitRequestOutcome(accepted=True, reply=reply, request_id=grant.request_id)


def _same_scope(grant: LimitGrant, request: LimitIncreaseRequest, scope: str) -> bool:
    if scope == RUN_SCOPE:
        return grant.execution_id == request.execution_id
    return request.period is not None and grant.period_start == request.period.start


def limit_alert_message(
    settings: Any,
    grant: LimitGrant,
    *,
    configured: float,
    effective: float,
    usage: float,
    band: Band,
) -> AlertMessage:
    lines = [
        ("Tenant", grant.tenant),
        ("Limit", grant.limit_name),
        ("Scope", f"run {grant.execution_id}" if grant.scope == RUN_SCOPE else "budget period"),
        ("Configured", _amount(configured)),
        ("Effective", _amount(effective)),
        ("Usage", _amount(usage)),
        ("Requested", _amount(grant.amount)),
        ("Band", band.describe(configured)),
        ("Requester", actor_words(grant.requester)),
    ]
    context = alert_context(settings, alert_ref(LIMIT_ALERT, grant.request_id))
    body = "\n".join(f"{label}: {value}" for label, value in lines)
    return AlertMessage(
        subject=f"[ADP] {LIMIT_ALERT_TITLE} by {grant.tenant}",
        body=f"{body}\n\nApprove or deny the request from the administrator Slack channel.",
        issue_key="",
        slack_text=f"{LIMIT_ALERT_TITLE} by {grant.tenant}",
        slack_blocks=labeled_blocks(
            LIMIT_ALERT_TITLE, lines, alert_buttons(context, LIMIT_ACTIONS)
        ),
    )


async def _audit_request(
    audit: Callable[[AuditEvent], Awaitable[Any]],
    request: LimitIncreaseRequest,
    scope: str,
    request_id: str,
    decision: str,
    reason: str,
) -> None:
    await audit(
        AuditEvent(
            event_id=f"{request.tenant}:{LIMIT_INCREASE_REQUESTED}:"
            f"{request_id or uuid.uuid4().hex}",
            task_id="",
            execution_id=(request.execution_id or f"limit:{request.tenant}")[:64],
            event_type=LIMIT_INCREASE_REQUESTED,
            decision=decision,
            reason=reason or None,
            actor_type=request.requester_type,
            actor_id=request.requester[:128],
            correlation_ids={
                "tenant": request.tenant,
                "request_id": request_id,
                "limit": request.limit_name,
                "scope": scope,
                "amount": request.amount,
            },
        )
    )


class LimitGrantDecisions:
    """Take an administrator's Approve or Deny click on a limit increase alert."""

    def __init__(
        self,
        *,
        store: LimitGrants,
        audit: Callable[[AuditEvent], Awaitable[Any]],
        posts: Any,
        resume: Callable[[LimitGrant], Awaitable[Sequence[str]]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._audit = audit
        self._posts = posts
        self._resume = resume
        self._clock = clock

    async def act(self, request: AlertRequest) -> AlertOutcome:
        if not request.admin:
            return AlertOutcome(refusal=Refusal.NOT_ALLOWED)
        state = _DECISIONS.get(request.action)
        if state is None:
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
        at = self._clock()
        decided = await self._store.decide(
            request.tenant, request.alert_id, state=state, approver=request.actor, at=at
        )
        if decided is None:
            existing = await self._store.get(request.tenant, request.alert_id)
            if existing is None:
                return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
            return AlertOutcome(
                reply=f"This request is already {existing.state} by "
                f"{actor_words(existing.approver)}.",
                refusal=Refusal.ALREADY_DECIDED,
            )
        await self._record(request, decided)
        await self._edit_post(request, decided, at)
        await self._notify_tenant(decided, request.actor, at)
        verb = "Granted" if state == GRANTED else "Denied"
        reply = (
            f"{verb} {_amount(decided.amount)} more of {decided.limit_name} for {decided.tenant}."
        )
        if state == GRANTED and self._resume is not None:
            resumed = await self._resume(decided)
            if resumed:
                reply += f" Resumed {', '.join(resumed)}."
        return AlertOutcome(reply=reply, applied=True)

    async def _edit_post(self, request: AlertRequest, grant: LimitGrant, at: datetime) -> None:
        if not request.message_ts:
            return
        owner_token = grant.admin_slack_token if request.message_ts == grant.admin_slack_ts else ""
        post = SlackPostRef(
            request.channel, request.message_ts, admin=True, owner_token=owner_token
        )
        blocks = list(request.message_blocks) or stored_blocks(grant.admin_slack_blocks) or []
        state = _DECISION_STATES[grant.state]
        await self._posts.update(
            post,
            text=f"{LIMIT_ALERT_TITLE}. {grant.state.capitalize()} by {actor_words(request.actor)}",
            blocks=status_blocks(blocks, state, actor=request.actor, at=at),
        )

    async def _notify_tenant(self, grant: LimitGrant, actor: str, at: datetime) -> None:
        """Move the tenant post the request was made on to the decision, and reply in its thread.

        A grant removes the post's buttons. A denial leaves the run parked, so the post keeps
        Abandon.
        """
        if not grant.tenant_slack_ts:
            return
        post = SlackPostRef(grant.tenant_slack_channel, grant.tenant_slack_ts)
        granted = grant.state == GRANTED
        verb = "Granted" if granted else "Denied"
        blocks = stored_blocks(grant.tenant_slack_blocks)
        if blocks:
            await self._posts.update(
                post,
                text=f"{verb} by {actor_words(actor)}",
                blocks=status_blocks(
                    blocks,
                    _DECISION_STATES[grant.state],
                    actor=actor,
                    at=at,
                    keep=frozenset() if granted else _DENIED_KEEPS,
                ),
            )
        run = grant.scope == RUN_SCOPE
        if granted:
            effect = "The run resumes." if run else "The parked runs resume."
        elif run:
            effect = "The run stays parked. Press Abandon to end it."
        else:
            effect = "The configured limit must change for more."
        await self._posts.reply(
            post,
            text=f"{verb} {_amount(grant.amount)} more of {grant.limit_name} by "
            f"{actor_words(actor)}. {effect}",
        )

    async def _record(self, request: AlertRequest, grant: LimitGrant) -> None:
        event = _DECISION_EVENTS[grant.state]
        await self._audit(
            AuditEvent(
                event_id=f"{grant.tenant}:{event}:{grant.request_id}",
                task_id="",
                execution_id=(grant.execution_id or f"limit:{grant.tenant}")[:64],
                event_type=event,
                decision=grant.state,
                actor_type=request.actor_type,
                actor_id=request.actor[:128],
                correlation_ids={
                    "tenant": grant.tenant,
                    "request_id": grant.request_id,
                    "limit": grant.limit_name,
                    "scope": grant.scope,
                    "amount": grant.amount,
                    "basis": request.basis,
                },
            )
        )


class LimitRequests:
    """Take a tenant's Request increase on a run limit park or on a spent period budget.

    The dispatcher verified the token the Acknowledge button of the same post carries, so the
    run, the park, and the usage are signed. A period budget request signs the period's key,
    and a request made after that period ended is refused. The amount is the option the tenant
    chose, and ``request_limit_increase`` refuses one outside the band.
    """

    def __init__(
        self,
        *,
        settings: Any,
        store: LimitGrants,
        audit: Callable[[AuditEvent], Awaitable[Any]],
        deliver: Callable[[AlertMessage, Any], list[str]] = deliver_admin_alert,
        period: Callable[[], Awaitable[BudgetPeriod | None]] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._audit = audit
        self._deliver = deliver
        self._period = period

    async def act(self, request: AlertRequest) -> AlertOutcome:
        ref = LimitRequestRef.parse(request.alert_id)
        if (
            request.subject != LIMIT_REQUEST_ALERT
            or request.action != REQUEST_LIMIT
            or request.admin
            or ref is None
            or ref.limit_name not in LIMIT_SETTINGS
        ):
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
        period: BudgetPeriod | None = None
        if limit_scope(ref.limit_name) == PERIOD_SCOPE:
            if request.execution_id or self._period is None:
                return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
            period = await self._period()
            if period is None or ref.park_key != period_request_key(request.tenant, period):
                return AlertOutcome(
                    reply="The budget period of this alert has ended. Request an increase from "
                    "the alert of the current period."
                )
        elif not request.execution_id or not ref.park_key.startswith(f"{request.execution_id}:"):
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
        try:
            amount = float(request.option)
        except ValueError:
            amount = math.nan
        if not math.isfinite(amount):
            return AlertOutcome(reply="Choose an amount under Request increase.")
        outcome = await request_limit_increase(
            LimitIncreaseRequest(
                tenant=request.tenant,
                limit_name=ref.limit_name,
                amount=amount,
                requester=request.actor,
                usage=ref.usage,
                execution_id=request.execution_id,
                period=period,
                requester_type=request.actor_type,
                tenant_post=SlackPostRef(
                    request.channel,
                    request.message_ts,
                    blocks=json.dumps(list(request.message_blocks)),
                )
                if request.message_ts
                else None,
            ),
            settings=self._settings,
            store=self._store,
            audit=self._audit,
            deliver=self._deliver,
        )
        return AlertOutcome(reply=outcome.reply, applied=outcome.accepted)
