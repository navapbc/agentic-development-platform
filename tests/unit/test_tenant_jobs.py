from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.slack.dispatcher import AlertRequest
from services.integration.slack.tokens import (
    LIMIT_REQUEST_ALERT,
    PAUSE_JOBS,
    REQUEST_LIMIT,
    RESUME_JOBS,
    TENANT_JOBS_ALERT,
    LimitRequestRef,
)
from services.orchestration.limit_grants import (
    GRANTED,
    PERIOD_SCOPE,
    REQUESTED,
    BudgetPeriod,
    LimitGrant,
    LimitRequests,
    period_request_key,
)
from services.orchestration.notifications import SlackPostRef
from services.orchestration.tenant_jobs import (
    TENANT_JOBS_PAUSED,
    TENANT_JOBS_RESUMED,
    TenantHold,
    TenantJobs,
    TenantPause,
    period_budget_message,
    read_period_budget,
)

ACTION_KEY = "k" * 32
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
PERIOD = BudgetPeriod(
    start=datetime(2026, 11, 1, tzinfo=UTC), reset=datetime(2026, 12, 1, tzinfo=UTC)
)
NEXT_PERIOD = BudgetPeriod(start=PERIOD.reset, reset=datetime(2027, 1, 1, tzinfo=UTC))
TENANT_USER = "slack:T1/UTENANT"
_MIGRATION = Path(__file__).parents[2] / "database/migrations/versions/0024_tenant_pause.py"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tenant="acme",
        escalation_action_key=ACTION_KEY,
        tenant_model_budget_usd=100.0,
        tenant_model_budget_duration="1mo",
        limit_grant_step_percent=25.0,
        limit_grant_period_percent=100.0,
    )


class _Team:
    def __init__(self, spend: float, reset: datetime = PERIOD.reset) -> None:
        self.spend = spend
        self.reset = reset

    async def team_info(self, team_id: str) -> dict[str, Any]:
        return {"spend": self.spend, "max_budget": 100.0, "budget_reset_at": self.reset.isoformat()}


class _Grants:
    def __init__(self, grants: list[LimitGrant] | None = None) -> None:
        self.rows = {grant.request_id: grant for grant in grants or []}

    async def create(self, grant: LimitGrant) -> None:
        self.rows[grant.request_id] = grant

    async def record_post(self, tenant: str, request_id: str, post: SlackPostRef) -> None:
        return None

    async def for_limit(self, tenant: str, limit_name: str) -> list[LimitGrant]:
        return [g for g in self.rows.values() if g.tenant == tenant and g.limit_name == limit_name]


class _Pauses:
    def __init__(self) -> None:
        self.current: TenantPause | None = None

    async def active(self, tenant: str) -> TenantPause | None:
        return self.current

    async def pause(self, tenant: str, actor: str, at: datetime) -> tuple[TenantPause, bool]:
        if self.current is not None:
            return self.current, False
        self.current = TenantPause(tenant, actor, at)
        return self.current, True

    async def clear(self, tenant: str, actor: str, at: datetime) -> TenantPause | None:
        cleared, self.current = self.current, None
        return cleared


class _Posts:
    def __init__(self) -> None:
        self.updates: list[list[dict[str, Any]]] = []

    async def update(self, post: SlackPostRef, *, text: str, blocks: list[dict[str, Any]]) -> bool:
        self.updates.append(blocks)
        return True


def _period_grant(amount: float, period: BudgetPeriod = PERIOD, **overrides: Any) -> LimitGrant:
    values: dict[str, Any] = {
        "request_id": f"req-{amount:g}-{period.start:%m}",
        "tenant": "acme",
        "limit_name": "TENANT_MODEL_BUDGET_USD",
        "scope": PERIOD_SCOPE,
        "amount": amount,
        "state": GRANTED,
        "requester": TENANT_USER,
        "period_start": period.start,
        "expires_at": period.reset,
    }
    return LimitGrant(**{**values, **overrides})


class _Hold:
    def __init__(self, team: _Team, grants: _Grants, pauses: _Pauses, ledger, alerts, resumes):
        self.alerts = alerts
        self.resumes = resumes

        async def _alert(budget) -> list[str]:
            alerts.append(budget)
            return ["slack"]

        async def _resume() -> list[str]:
            resumes.append("resumed")
            return ["wf-parked"]

        self.hold = TenantHold(
            tenant="acme",
            pauses=pauses,
            budget=lambda: read_period_budget(_settings(), grants=grants, client=team, now=NOW),
            alert=_alert,
            resume=_resume,
            ledger=ledger,
        )


async def _admit(team: _Team, grants: _Grants, ledger, alerts, resumes, pauses=None) -> str:
    return await _Hold(team, grants, pauses or _Pauses(), ledger, alerts, resumes).hold.admit()


async def test_a_spent_period_budget_defers_intake_and_alerts_once_per_period():
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []
    team = _Team(spend=100.0)

    first = await _admit(team, _Grants(), ledger, alerts, resumes)
    second = await _admit(team, _Grants(), ledger, alerts, resumes)
    next_period = await _admit(
        _Team(spend=100.0, reset=NEXT_PERIOD.reset), _Grants(), ledger, alerts, resumes
    )

    assert first == second
    assert first.startswith("tenant_budget_spent: TENANT_MODEL_BUDGET_USD is spent at 100 of 100")
    assert next_period.startswith("tenant_budget_spent")
    assert [budget.period.start for budget in alerts] == [PERIOD.start, NEXT_PERIOD.start]
    assert resumes == []


@pytest.mark.parametrize(
    ("spend", "grants"),
    [(100.0, [_period_grant(10.0)]), (0.0, [])],
    ids=["period grant", "period reset"],
)
async def test_a_period_grant_or_the_period_reset_resumes_the_parks_and_admits_intake(
    spend, grants
):
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []

    reason = await _admit(_Team(spend=spend), _Grants(grants), ledger, alerts, resumes)

    assert reason == ""
    assert resumes == ["resumed"]
    assert alerts == []


async def test_a_grant_of_an_earlier_period_does_not_raise_the_current_budget():
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []
    earlier = BudgetPeriod(start=datetime(2026, 10, 1, tzinfo=UTC), reset=PERIOD.start)

    reason = await _admit(
        _Team(spend=100.0), _Grants([_period_grant(10.0, earlier)]), ledger, alerts, resumes
    )

    assert reason.startswith("tenant_budget_spent")
    assert resumes == []


def _select(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    [select] = [
        element
        for block in blocks
        if block.get("type") == "actions"
        for element in block["elements"]
        if element["action_id"] == REQUEST_LIMIT
    ]
    return select


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        element["action_id"]
        for block in blocks
        if block.get("type") == "actions"
        for element in block["elements"]
    ]


async def test_request_increase_on_the_period_budget_offers_the_band_left_in_the_period():
    budget = await read_period_budget(
        _settings(), grants=_Grants(), client=_Team(spend=100.0), now=NOW
    )
    grants = _Grants([_period_grant(90.0), _period_grant(50.0, NEXT_PERIOD)])

    message = await period_budget_message(_settings(), budget, grants=grants)

    assert [option["value"] for option in _select(message.slack_blocks)["options"]] == ["10"]
    assert _action_ids(message.slack_blocks) == [REQUEST_LIMIT, "acknowledge", PAUSE_JOBS]


def _request(park_key: str, option: str = "10") -> AlertRequest:
    return AlertRequest(
        tenant="acme",
        subject=LIMIT_REQUEST_ALERT,
        alert_id=LimitRequestRef(usage=100.0, park_key=park_key).alert_id,
        action=REQUEST_LIMIT,
        actor=TENANT_USER,
        user="UTENANT",
        basis="channel",
        channel="CTENANT",
        message_ts="",
        message_blocks=(),
        option=option,
    )


async def test_a_period_request_reaches_the_administrators_and_an_ended_period_is_refused():
    store = _Grants()
    audits: list[Any] = []

    async def audit(event: Any) -> None:
        audits.append(event)

    async def current() -> BudgetPeriod:
        return PERIOD

    requests = LimitRequests(
        settings=_settings(),
        store=store,
        audit=audit,
        deliver=lambda message, settings: ["slack"],
        period=current,
    )

    accepted = await requests.act(_request(period_request_key("acme", PERIOD)))
    ended = await requests.act(
        _request(
            period_request_key(
                "acme", BudgetPeriod(start=datetime(2026, 10, 1, tzinfo=UTC), reset=PERIOD.start)
            )
        )
    )

    assert accepted.applied
    [grant] = store.rows.values()
    assert (grant.scope, grant.state, grant.amount) == (PERIOD_SCOPE, REQUESTED, 10.0)
    assert (grant.period_start, grant.expires_at) == (PERIOD.start, PERIOD.reset)
    assert not ended.applied
    assert "has ended" in ended.reply
    assert len(store.rows) == 1


def _jobs_click(action: str, blocks: list[dict[str, Any]]) -> AlertRequest:
    return AlertRequest(
        tenant="acme",
        subject=TENANT_JOBS_ALERT,
        alert_id=str(int(PERIOD.start.timestamp())),
        action=action,
        actor=TENANT_USER,
        user="UTENANT",
        basis="channel",
        channel="CTENANT",
        message_ts="170.1",
        message_blocks=blocks,
    )


class _Jobs:
    def __init__(self) -> None:
        self.pauses = _Pauses()
        self.posts = _Posts()
        self.audits: list[Any] = []
        self.intake_triggers = 0

        async def audit(event: Any) -> None:
            self.audits.append(event)

        async def trigger() -> None:
            self.intake_triggers += 1

        self.jobs = TenantJobs(
            settings=_settings(),
            pauses=self.pauses,
            audit=audit,
            posts=self.posts,
            trigger_intake=trigger,
            clock=lambda: NOW,
        )


async def _alert_blocks() -> list[dict[str, Any]]:
    budget = await read_period_budget(
        _settings(), grants=_Grants(), client=_Team(spend=100.0), now=NOW
    )
    return (await period_budget_message(_settings(), budget, grants=_Grants())).slack_blocks


async def test_park_all_jobs_defers_intake_and_leaves_the_running_runs_alone():
    jobs = _Jobs()
    blocks = await _alert_blocks()

    outcome = await jobs.jobs.act(_jobs_click(PAUSE_JOBS, blocks))
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []
    reason = await _admit(_Team(spend=0.0), _Grants(), ledger, alerts, resumes, jobs.pauses)

    assert outcome.applied
    assert reason.startswith("tenant_paused: Park all jobs by")
    assert resumes == ["resumed"]
    assert [event.event_type for event in jobs.audits] == [TENANT_JOBS_PAUSED]
    [edited] = jobs.posts.updates
    assert _action_ids(edited) == [REQUEST_LIMIT, "acknowledge", RESUME_JOBS]
    assert any(block.get("block_id") == "adp-jobs_paused" for block in edited)


async def test_a_grant_does_not_clear_a_manual_pause():
    jobs = _Jobs()
    await jobs.jobs.act(_jobs_click(PAUSE_JOBS, await _alert_blocks()))
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []

    reason = await _admit(
        _Team(spend=100.0), _Grants([_period_grant(10.0)]), ledger, alerts, resumes, jobs.pauses
    )

    assert reason.startswith("tenant_paused")
    assert jobs.pauses.current is not None


async def test_resume_jobs_clears_the_pause_and_starts_intake_again():
    jobs = _Jobs()
    await jobs.jobs.act(_jobs_click(PAUSE_JOBS, await _alert_blocks()))

    resumed = await jobs.jobs.act(_jobs_click(RESUME_JOBS, jobs.posts.updates[-1]))
    again = await jobs.jobs.act(_jobs_click(RESUME_JOBS, jobs.posts.updates[-1]))
    ledger, alerts, resumes = InMemorySideEffectLedger(), [], []
    reason = await _admit(_Team(spend=0.0), _Grants(), ledger, alerts, resumes, jobs.pauses)

    assert resumed.applied
    assert not again.applied
    assert reason == ""
    assert jobs.intake_triggers == 1
    assert [event.event_type for event in jobs.audits] == [TENANT_JOBS_PAUSED, TENANT_JOBS_RESUMED]
    assert _action_ids(jobs.posts.updates[-1]) == [REQUEST_LIMIT, "acknowledge", PAUSE_JOBS]


class _CreateOp:
    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        self.indexes: list[tuple[str, str, list[str], dict[str, Any]]] = []
        self.dropped: list[str] = []

    def create_table(self, name: str, *columns: Any, schema: str) -> None:
        sa.Table(name, self.metadata, *columns, schema=schema)

    def create_index(self, name: str, table: str, columns: list[str], **kwargs: Any) -> None:
        self.indexes.append((name, table, columns, kwargs))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.dropped.append(name)

    def drop_table(self, name: str, **kwargs: Any) -> None:
        self.dropped.append(name)


def test_the_tenant_pause_migration_creates_the_table_the_schema_declares(monkeypatch):
    from database.models.schema import tenant_pauses

    spec = importlib.util.spec_from_file_location("tenant_pause_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    op = _CreateOp()
    monkeypatch.setattr(module, "op", op)

    module.upgrade()
    module.downgrade()

    created = op.metadata.tables["agent_platform.tenant_pauses"]
    assert module.down_revision == "0023_limit_grants"
    assert [(c.name, c.nullable) for c in created.columns] == [
        (c.name, c.nullable) for c in tenant_pauses.columns
    ]
    [(name, _, columns, kwargs)] = op.indexes
    [declared] = tenant_pauses.indexes
    assert (name, columns, kwargs["unique"]) == (declared.name, ["tenant"], True)
    assert str(kwargs["postgresql_where"]) == str(declared.dialect_options["postgresql"]["where"])
    assert op.dropped == [name, "tenant_pauses"]


async def test_a_second_park_on_the_same_period_reads_the_one_period_alert_post():
    from services.orchestration.tenant_jobs import ensure_period_alert

    ledger = InMemorySideEffectLedger()
    budget = await read_period_budget(_settings(), grants=_Grants(), client=_Team(100.0), now=NOW)
    sent: list[Any] = []

    async def send(spent) -> dict[str, Any]:
        sent.append(spent)
        return {"channels": ["slack"], "channel": "CTENANT", "ts": "100.1", "blocks": ""}

    first = await ensure_period_alert(ledger, "acme", budget, send)
    second = await ensure_period_alert(ledger, "acme", budget, send)

    assert len(sent) == 1
    assert (first.ts, second.ts, second.reason) == ("100.1", "100.1", "")


async def test_a_period_alert_in_flight_past_the_wait_bound_names_why_the_park_posts_alone():
    from services.coordination.idempotency import SideEffectOutcome
    from services.orchestration.tenant_jobs import PERIOD_ALERT_WAIT_ATTEMPTS, ensure_period_alert

    class _InFlight:
        async def run_once(self, ref, effect):
            return SideEffectOutcome("key", replayed=False, result=None, in_flight=True)

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    budget = await read_period_budget(_settings(), grants=_Grants(), client=_Team(100.0), now=NOW)
    post = await ensure_period_alert(_InFlight(), "acme", budget, send=None, sleep=sleep)

    assert post.ts == ""
    assert post.reason == "the period alert was still being posted after 8 seconds"
    assert len(sleeps) == PERIOD_ALERT_WAIT_ATTEMPTS - 1


class _ThreadPosts(_Posts):
    def __init__(self) -> None:
        super().__init__()
        self.replies: list[tuple[SlackPostRef, str]] = []

    async def reply(self, post: SlackPostRef, *, text: str) -> bool:
        self.replies.append((post, text))
        return True


async def test_the_period_reset_edits_the_previous_period_alert_once_and_replies_in_its_thread():
    from services.orchestration.tenant_jobs import period_alert_ref, settle_period_alert

    ledger = InMemorySideEffectLedger()
    blocks = await _alert_blocks()
    await ledger.record(
        period_alert_ref("acme", PERIOD, 110.0),
        {"channel": "CTENANT", "ts": "100.1", "blocks": json.dumps(blocks)},
    )
    posts = _ThreadPosts()
    current = await read_period_budget(
        _settings(), grants=_Grants(), client=_Team(0.0, reset=NEXT_PERIOD.reset), now=NOW
    )

    grants = _Grants([_period_grant(10.0)])
    settle = {"grants": grants, "configured": 100.0}

    first = await settle_period_alert(ledger, "acme", current, "1mo", posts, **settle)
    second = await settle_period_alert(ledger, "acme", current, "1mo", posts, **settle)

    assert (first, second) == (True, False)
    [edited] = posts.updates
    assert any(block.get("block_id") == "adp-period_reset" for block in edited)
    assert _action_ids(edited) == []
    [(post, text)] = posts.replies
    assert post.ts == "100.1"
    assert text.startswith("The budget period reset.")


async def test_an_unspent_budget_settles_the_previous_period_alert_after_the_resume():
    settled: list[Any] = []
    calls: list[str] = []

    async def resume() -> list[str]:
        calls.append("resume")
        return []

    async def settle(budget) -> None:
        calls.append("settle")
        settled.append(budget)

    hold = TenantHold(
        tenant="acme",
        pauses=_Pauses(),
        budget=lambda: read_period_budget(
            _settings(), grants=_Grants(), client=_Team(0.0), now=NOW
        ),
        alert=None,
        resume=resume,
        ledger=InMemorySideEffectLedger(),
        settle=settle,
    )

    assert await hold.admit() == ""
    assert calls == ["resume", "settle"]
