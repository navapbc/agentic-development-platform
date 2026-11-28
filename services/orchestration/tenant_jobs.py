"""The tenant period budget hold and the Park all jobs pause that intake checks on every pass.

The tenant model budget TENANT_MODEL_BUDGET_USD is spent when the LiteLLM team spend reaches the
configured budget plus the period grants. While it is spent, intake defers every ticket, and one
alert per budget period reaches the tenant with Request increase, Park all jobs, and Acknowledge.
A pass that finds the budget no longer spent, after a period grant or the period reset, resumes
the runs parked on the budget.

Park all jobs records a pause in ``tenant_pauses``. While the pause is set intake defers every
ticket and running runs finish. Only Resume jobs clears it, so a grant or a period reset leaves a
manual pause in place.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.integration.slack.dispatcher import AlertOutcome, AlertRequest, Refusal
from services.integration.slack.messages import (
    PostState,
    actions_block,
    actor_words,
    alert_buttons,
    labeled_blocks,
    limit_request_elements,
    status_blocks,
)
from services.integration.slack.tokens import (
    ACKNOWLEDGE,
    LIMIT_REQUEST_ALERT,
    PAUSE_JOBS,
    REQUEST_LIMIT,
    RESUME_JOBS,
    TENANT_JOBS_ALERT,
    LimitRequestRef,
    alert_ref,
)
from services.orchestration.limit_grants import (
    GRANTED,
    PERIOD_SCOPE,
    BudgetPeriod,
    LimitGrants,
    effective_value,
    period_request_key,
    request_amounts,
    tenant_budget_period,
)
from services.orchestration.notifications import (
    AlertMessage,
    SlackPostRef,
    alert_context,
    deliver_alert,
    stored_blocks,
)
from services.orchestration.temporal_activities.persistence import AuditEvent

log = structlog.get_logger("tenant_jobs")

TENANT_BUDGET = "TENANT_MODEL_BUDGET_USD"
TENANT_JOBS_PAUSED = "TENANT_JOBS_PAUSED"
TENANT_JOBS_RESUMED = "TENANT_JOBS_RESUMED"
PERIOD_RESET_ACTOR = "period_reset"
PERIOD_BUDGET_ALERT_TITLE = "Tenant model budget spent"
PERIOD_BUDGET_NEXT_STEP = (
    "Choose an amount under Request increase to ask the administrators for more of the period "
    "budget, press Park all jobs to hold intake until Resume jobs, or press Acknowledge to show "
    "the alert is seen."
)
_KEPT_ON_TOGGLE = frozenset({REQUEST_LIMIT, ACKNOWLEDGE, PostState.ABANDONED.value})
_TOGGLE = {PAUSE_JOBS: RESUME_JOBS, RESUME_JOBS: PAUSE_JOBS}


@dataclass(frozen=True)
class PeriodBudget:
    configured: float
    effective: float
    spend: float
    period: BudgetPeriod | None

    @property
    def spent(self) -> bool:
        return self.configured > 0 and self.spend >= self.effective

    @property
    def usage(self) -> str:
        return f"{self.spend:g} of {self.effective:g} USD"


async def read_period_budget(
    settings: Any, *, grants: LimitGrants, client: Any = None, now: datetime | None = None
) -> PeriodBudget | None:
    """Return the tenant's period budget from its LiteLLM team, or None without a budget or team.

    The LiteLLM team record holds the spend and the ``budget_reset_at`` the period is read from.
    """
    configured = float(settings.tenant_model_budget_usd)
    tenant = settings.tenant.strip()
    if configured <= 0 or not tenant:
        return None
    from services.integration.litellm_admin import LiteLLMAdminClient

    admin = client or LiteLLMAdminClient.from_settings(settings)
    team = await admin.team_info(tenant)
    if team is None:
        return None
    return PeriodBudget(
        configured=configured,
        effective=await effective_value(grants, tenant, TENANT_BUDGET, configured, now=now),
        spend=float(team.get("spend") or 0.0),
        period=tenant_budget_period(team, settings.tenant_model_budget_duration),
    )


def period_budget_deferral_reason(budget: PeriodBudget) -> str:
    reset = f" until {budget.period.reset:%Y-%m-%d %H:%M} UTC" if budget.period else ""
    return f"tenant_budget_spent: {TENANT_BUDGET} is spent at {budget.usage}{reset}"


def tenant_paused_reason(pause: TenantPause) -> str:
    return f"tenant_paused: Park all jobs by {actor_words(pause.paused_by)} holds intake"


async def period_budget_message(
    settings: Any, budget: PeriodBudget, *, grants: LimitGrants
) -> AlertMessage:
    """Return the tenant alert of a spent period budget.

    Request increase offers the in-band amounts of the period, signed on the period's key. Park
    all jobs is signed on the tenant jobs alert of the same period.
    """
    tenant = settings.tenant.strip()
    elements: list[dict[str, Any]] = []
    jobs_id = "unknown"
    if budget.period is not None:
        jobs_id = str(int(budget.period.start.timestamp()))
        amounts = await request_amounts(
            settings, grants, TENANT_BUDGET, execution_id="", period=budget.period
        )
        ref = LimitRequestRef(
            usage=budget.spend, park_key=period_request_key(tenant, budget.period)
        )
        elements = limit_request_elements(
            alert_context(settings, alert_ref(LIMIT_REQUEST_ALERT, ref.alert_id)), amounts
        )
    jobs_actions = (PAUSE_JOBS,) if elements else (ACKNOWLEDGE, PAUSE_JOBS)
    elements += alert_buttons(
        alert_context(settings, alert_ref(TENANT_JOBS_ALERT, jobs_id)), jobs_actions
    )
    lines = [
        ("Tenant", tenant),
        ("Limit", TENANT_BUDGET),
        ("Usage", budget.usage),
        ("Period reset", f"{budget.period.reset:%Y-%m-%d %H:%M} UTC" if budget.period else ""),
        (
            "Effect",
            "Intake defers every ticket, and running runs park at their next stage boundary "
            "until the period resets or an administrator grants more.",
        ),
        ("Next step", PERIOD_BUDGET_NEXT_STEP),
    ]
    body = "\n".join(f"{label}: {value}" for label, value in lines if value)
    return AlertMessage(
        subject=f"[ADP] {PERIOD_BUDGET_ALERT_TITLE} for {tenant}",
        body=body,
        issue_key="",
        slack_text=f"{PERIOD_BUDGET_ALERT_TITLE} for {tenant}",
        slack_blocks=labeled_blocks(PERIOD_BUDGET_ALERT_TITLE, lines, elements),
    )


@dataclass(frozen=True)
class TenantPause:
    tenant: str
    paused_by: str
    paused_at: datetime


class TenantPauses(Protocol):
    async def active(self, tenant: str) -> TenantPause | None: ...

    async def pause(self, tenant: str, actor: str, at: datetime) -> tuple[TenantPause, bool]: ...

    async def clear(self, tenant: str, actor: str, at: datetime) -> TenantPause | None: ...


class PostgresTenantPauses:
    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def _get_engine(self) -> Any:
        if self._engine is None:
            from database.engine import shared_platform_engine

            self._engine = shared_platform_engine()
        return self._engine

    async def active(self, tenant: str) -> TenantPause | None:
        from database.models.schema import tenant_pauses
        from sqlalchemy import select

        stmt = select(tenant_pauses).where(
            tenant_pauses.c.tenant == tenant, tenant_pauses.c.cleared_at.is_(None)
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        return TenantPause(row.tenant, row.paused_by, row.paused_at) if row else None

    async def pause(self, tenant: str, actor: str, at: datetime) -> tuple[TenantPause, bool]:
        """Set the tenant's pause, returning the active pause and whether this call set it."""
        from database.models.schema import tenant_pauses
        from sqlalchemy import text
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(tenant_pauses)
            .values(pause_id=uuid.uuid4().hex, tenant=tenant, paused_by=actor[:128], paused_at=at)
            .on_conflict_do_nothing(
                index_elements=["tenant"], index_where=text("cleared_at IS NULL")
            )
        )
        async with self._get_engine().begin() as conn:
            created = (await conn.execute(stmt)).rowcount == 1
        active = await self.active(tenant)
        return active or TenantPause(tenant, actor, at), created

    async def clear(self, tenant: str, actor: str, at: datetime) -> TenantPause | None:
        """Clear the tenant's pause, returning the cleared pause or None when none was set."""
        from database.models.schema import tenant_pauses
        from sqlalchemy import update

        stmt = (
            update(tenant_pauses)
            .where(tenant_pauses.c.tenant == tenant, tenant_pauses.c.cleared_at.is_(None))
            .values(cleared_by=actor[:128], cleared_at=at)
            .returning(tenant_pauses.c.paused_by, tenant_pauses.c.paused_at)
        )
        async with self._get_engine().begin() as conn:
            row = (await conn.execute(stmt)).first()
        return TenantPause(tenant, row.paused_by, row.paused_at) if row else None


PERIOD_ALERT_WAIT_ATTEMPTS = 5
PERIOD_ALERT_WAIT_SECONDS = 2.0


def period_alert_ref(tenant: str, period: BudgetPeriod | None, effective: float) -> SideEffectRef:
    """Return the ledger entry of a budget period's alert, whose result holds its Slack post.

    The entry is keyed on the period and on the effective budget that ran out, so the budget
    running out again after a period grant sends a new alert.
    """
    start = period.start.isoformat() if period else "unknown"
    return SideEffectRef(
        task_id=f"tenant-budget@{tenant}",
        execution_id="",
        operation="period_budget_alert",
        target=f"{start}:{effective:g}",
    )


@dataclass(frozen=True)
class PeriodAlertPost:
    channel: str = ""
    ts: str = ""
    reason: str = ""
    """Why a run parked on the period budget posts at the top level, or empty for the thread."""


async def ensure_period_alert(
    ledger: SideEffectLedger,
    tenant: str,
    budget: PeriodBudget,
    send: Callable[[PeriodBudget], Awaitable[dict[str, Any]]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PeriodAlertPost:
    """Post the period's alert once through the ledger and return its Slack post.

    A caller that finds the alert in flight with another caller waits a bounded time for it.
    """
    ref = period_alert_ref(tenant, budget.period, budget.effective)
    for attempt in range(PERIOD_ALERT_WAIT_ATTEMPTS):
        outcome = await ledger.run_once(ref, lambda: send(budget))
        if not outcome.in_flight:
            recorded = outcome.result if isinstance(outcome.result, dict) else {}
            ts = str(recorded.get("ts") or "")
            return PeriodAlertPost(
                channel=str(recorded.get("channel") or ""),
                ts=ts,
                reason="" if ts else "the period alert reached no Slack post",
            )
        if attempt + 1 < PERIOD_ALERT_WAIT_ATTEMPTS:
            await sleep(PERIOD_ALERT_WAIT_SECONDS)
    waited = PERIOD_ALERT_WAIT_ATTEMPTS * PERIOD_ALERT_WAIT_SECONDS - PERIOD_ALERT_WAIT_SECONDS
    return PeriodAlertPost(
        reason=f"the period alert was still being posted after {waited:g} seconds"
    )


async def settle_period_alert(
    ledger: SideEffectLedger,
    tenant: str,
    budget: PeriodBudget,
    duration: str,
    posts: Any,
    *,
    grants: LimitGrants,
    configured: float,
) -> bool:
    """Edit the previous period's last alert to show the reset and reply in its thread, once.

    The last alert is the one keyed on the configured budget plus every grant of that period.
    An earlier alert of the period was moved to Granted by the grant that followed it.
    Returns whether this call edited it.
    """
    if budget.period is None:
        return False
    previous = tenant_budget_period({"budget_reset_at": budget.period.start.isoformat()}, duration)
    if previous is None:
        return False
    granted = sum(
        grant.amount
        for grant in await grants.for_limit(tenant, TENANT_BUDGET)
        if grant.state == GRANTED
        and grant.scope == PERIOD_SCOPE
        and grant.period_start == previous.start
    )
    last_alert = period_alert_ref(tenant, previous, configured + granted)
    recorded = await ledger.lookup(last_alert)
    if not isinstance(recorded, dict) or not recorded.get("ts"):
        return False
    post = SlackPostRef(str(recorded.get("channel") or ""), str(recorded["ts"]))
    blocks = stored_blocks(str(recorded.get("blocks") or ""))
    edited: list[bool] = []

    async def _edit() -> dict[str, bool]:
        if blocks:
            await posts.update(
                post,
                text=f"{PERIOD_BUDGET_ALERT_TITLE}. The budget period reset",
                blocks=status_blocks(blocks, PostState.PERIOD_RESET, at=datetime.now(UTC)),
            )
        await posts.reply(
            post, text="The budget period reset. The parked runs resume and intake starts again."
        )
        edited.append(True)
        return {"edited": True}

    await ledger.run_once(replace(last_alert, operation="period_budget_reset"), _edit)
    return bool(edited)


class TenantHold:
    """Decide once per intake pass whether the tenant's pause or period budget holds intake.

    A pass that finds the budget spent sends the period's alert through the side-effect ledger,
    so each spent period alerts once. A pass that finds it unspent resumes the budget parks.
    """

    def __init__(
        self,
        *,
        tenant: str,
        pauses: TenantPauses,
        budget: Callable[[], Awaitable[PeriodBudget | None]],
        alert: Callable[[PeriodBudget], Awaitable[dict[str, Any]]],
        resume: Callable[[], Awaitable[Sequence[str]]],
        ledger: SideEffectLedger,
        settle: Callable[[PeriodBudget], Awaitable[Any]] | None = None,
    ) -> None:
        self._tenant = tenant
        self._pauses = pauses
        self._budget = budget
        self._alert = alert
        self._resume = resume
        self._ledger = ledger
        self._settle = settle
        self._reason: str | None = None

    async def admit(self) -> str:
        """Return why intake defers every ticket this pass, or an empty string to admit them."""
        if self._reason is None:
            self._reason = await self._check()
        return self._reason

    async def _check(self) -> str:
        pause = await self._pauses.active(self._tenant)
        try:
            budget = await self._budget()
        except Exception:  # noqa: BLE001
            log.warning("tenant_budget_read_failed", tenant=self._tenant, exc_info=True)
            budget = None
        if budget is not None and budget.spent:
            await self._alert_once(budget)
        elif budget is not None:
            resumed = await self._resume()
            if resumed:
                log.info("tenant_budget_parks_resumed", tenant=self._tenant, workflow_ids=resumed)
            if self._settle is not None:
                try:
                    await self._settle(budget)
                except Exception:  # noqa: BLE001
                    log.warning("period_alert_settle_failed", tenant=self._tenant, exc_info=True)
        if pause is not None:
            return tenant_paused_reason(pause)
        if budget is not None and budget.spent:
            return period_budget_deferral_reason(budget)
        return ""

    async def _alert_once(self, budget: PeriodBudget) -> None:
        try:
            await self._ledger.run_once(
                period_alert_ref(self._tenant, budget.period, budget.effective),
                lambda: self._alert(budget),
            )
        except Exception:  # noqa: BLE001
            log.warning("period_budget_alert_failed", tenant=self._tenant, exc_info=True)


async def send_period_alert(
    settings: Any, budget: PeriodBudget, *, grants: LimitGrants
) -> dict[str, Any]:
    """Deliver the period's alert and return the channels and the Slack post the ledger keeps."""
    message = await period_budget_message(settings, budget, grants=grants)
    channels = await asyncio.to_thread(deliver_alert, message, settings)
    post = next((post for post in message.posts if not post.admin), None)
    return {
        "channels": list(channels),
        "channel": post.channel if post else "",
        "ts": post.ts if post else "",
        "blocks": post.blocks if post else "",
    }


def build_tenant_hold(settings: Any, ledger: SideEffectLedger, *, client: Any) -> TenantHold | None:
    """Return the intake hold of the worker's tenant, or None without a tenant."""
    tenant = settings.tenant.strip()
    if not tenant:
        return None
    from services.orchestration.alert_actions import SlackPosts
    from services.orchestration.limit_grants import PostgresLimitGrants
    from services.orchestration.limit_parks import (
        PostgresLimitParkedRuns,
        TemporalLimitParkResumer,
    )
    from services.orchestration.model_path_parks import resume_each

    grants = PostgresLimitGrants()
    runs = PostgresLimitParkedRuns()
    resumer = TemporalLimitParkResumer(client)

    async def _alert(budget: PeriodBudget) -> dict[str, Any]:
        return await send_period_alert(settings, budget, grants=grants)

    async def _resume() -> list[str]:
        return await resume_each(
            await runs.find(tenant, limit_name=TENANT_BUDGET),
            lambda workflow_id: resumer.resume(
                workflow_id,
                limit_name=TENANT_BUDGET,
                actor=PERIOD_RESET_ACTOR,
                actor_type=PERIOD_RESET_ACTOR,
            ),
        )

    return TenantHold(
        tenant=tenant,
        pauses=PostgresTenantPauses(),
        budget=lambda: read_period_budget(settings, grants=grants),
        alert=_alert,
        resume=_resume,
        ledger=ledger,
        settle=lambda budget: settle_period_alert(
            ledger,
            tenant,
            budget,
            settings.tenant_model_budget_duration,
            SlackPosts(settings),
            grants=grants,
            configured=float(settings.tenant_model_budget_usd),
        ),
    )


def _toggled(
    blocks: Sequence[dict[str, Any]], state: PostState, actor: str, at: datetime, button: list
) -> list[dict[str, Any]]:
    moved = status_blocks(blocks, state, actor=actor, at=at, keep=_KEPT_ON_TOGGLE)
    for index, block in enumerate(moved):
        if block.get("type") == "actions":
            moved[index] = {**block, "elements": [*block["elements"], *button]}
            return moved
    return [*moved, *actions_block(button)]


class TenantJobs:
    """Take a tenant's Park all jobs or Resume jobs click on a period budget alert."""

    def __init__(
        self,
        *,
        settings: Any,
        pauses: TenantPauses,
        audit: Callable[[AuditEvent], Awaitable[Any]],
        posts: Any,
        trigger_intake: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._tenant = settings.tenant.strip()
        self._pauses = pauses
        self._audit = audit
        self._posts = posts
        self._trigger_intake = trigger_intake
        self._clock = clock

    async def act(self, request: AlertRequest) -> AlertOutcome:
        if request.admin or request.subject != TENANT_JOBS_ALERT or request.action not in _TOGGLE:
            return AlertOutcome(refusal=Refusal.NOT_PERMITTED)
        at = self._clock()
        if request.action == PAUSE_JOBS:
            pause, created = await self._pauses.pause(self._tenant, request.actor, at)
            if not created:
                return AlertOutcome(
                    reply=f"Jobs are already parked by {actor_words(pause.paused_by)}."
                )
            await self._record(request, TENANT_JOBS_PAUSED, "paused")
            await self._edit_post(request, PostState.JOBS_PAUSED, at)
            return AlertOutcome(
                reply=f"Jobs parked by {actor_words(request.actor)}. Intake defers every ticket "
                "and running runs finish. Press Resume jobs to start intake again.",
                applied=True,
            )
        cleared = await self._pauses.clear(self._tenant, request.actor, at)
        if cleared is None:
            return AlertOutcome(reply="Jobs are not parked.")
        await self._record(request, TENANT_JOBS_RESUMED, "resumed")
        await self._edit_post(request, PostState.JOBS_RESUMED, at)
        if self._trigger_intake is not None:
            await self._trigger_intake()
        return AlertOutcome(
            reply=f"Jobs resumed by {actor_words(request.actor)}. Intake starts again.",
            applied=True,
        )

    async def _edit_post(self, request: AlertRequest, state: PostState, at: datetime) -> None:
        if not request.message_ts or not request.message_blocks:
            return
        context = alert_context(
            self._settings,
            alert_ref(TENANT_JOBS_ALERT, request.alert_id),
            issued_at=int(at.timestamp()),
        )
        button = alert_buttons(context, (_TOGGLE[request.action],))
        await self._posts.update(
            SlackPostRef(request.channel, request.message_ts),
            text=f"{PERIOD_BUDGET_ALERT_TITLE}. {state.value.replace('_', ' ').capitalize()} by "
            f"{actor_words(request.actor)}",
            blocks=_toggled(request.message_blocks, state, request.actor, at, button),
        )

    async def _record(self, request: AlertRequest, event: str, decision: str) -> None:
        await self._audit(
            AuditEvent(
                event_id=f"{self._tenant}:{event}:{uuid.uuid4().hex}",
                task_id="",
                execution_id=f"tenant:{self._tenant}"[:64],
                event_type=event,
                decision=decision,
                actor_type=request.actor_type,
                actor_id=request.actor[:128],
                correlation_ids={
                    "tenant": self._tenant,
                    "alert": request.alert_id,
                    "basis": request.basis,
                },
            )
        )


__all__ = [
    "PERIOD_BUDGET_ALERT_TITLE",
    "PERIOD_RESET_ACTOR",
    "TENANT_BUDGET",
    "TENANT_JOBS_PAUSED",
    "TENANT_JOBS_RESUMED",
    "PeriodBudget",
    "PostgresTenantPauses",
    "TenantHold",
    "TenantJobs",
    "TenantPause",
    "build_tenant_hold",
    "period_budget_deferral_reason",
    "period_budget_message",
    "read_period_budget",
    "tenant_paused_reason",
]
