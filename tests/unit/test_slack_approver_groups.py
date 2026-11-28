from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from services.integration.slack import approver_groups
from services.integration.slack.approver_groups import (
    AdminGroupRefresh,
    ApproverGroupRefresh,
    PlatformGroupReader,
    PostgresApproverGroupStore,
    PostgresRefreshLock,
    SlackGroupReader,
    keep_approver_groups,
)
from services.integration.slack.poster_client import PostingFailed
from services.orchestration.escalation import (
    EMAIL,
    OWN_APP,
    SHARED_APP,
    Approvers,
    EscalationConfig,
    SlackGroup,
    SlackSettings,
    SlackUser,
)
from services.orchestration.platform_escalation import ADMIN_GROUP_SCOPE, AdminDestination
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse
from sqlalchemy.dialects import postgresql
from tests.support import hermetic_settings

ORGANIZATION = "E0ORG"
GROUP = "S0123456789"
OTHER_GROUP = "S0999999999"
READ_AT = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)
API_BASE_URL = "https://slack-gov.com/api/"


def _config(
    *group_ids: str,
    app: str = OWN_APP,
    channels: tuple[str, ...] | None = None,
    team_id: str = "",
):
    slack = SlackSettings(
        transport="webhook",
        channel="C0ADP",
        organization_id=ORGANIZATION,
        app_id="A0ADP",
        profile="private",
        content="full",
        api_base_url=API_BASE_URL,
        allowlist={
            "gate": Approvers(anyone_in_channel=True),
            "repair": Approvers(
                users=(SlackUser(ORGANIZATION, "U0NAMED"),),
                groups=tuple(SlackGroup(ORGANIZATION, group_id, team_id) for group_id in group_ids),
            ),
        },
        app=app,
    )
    if channels is None:
        return EscalationConfig(slack=slack)
    return EscalationConfig(channels=channels, slack=slack)


class _Store:
    def __init__(self, records: dict[tuple[str, str], tuple[list[str], datetime]] | None = None):
        self.records = dict(records or {})
        self.forgets = 0

    async def record(self, tenant, group_id, members, refreshed_at) -> None:
        self.records[(tenant, group_id)] = (list(members), refreshed_at)

    async def forget_unnamed(self, tenant, named) -> None:
        self.forgets += 1
        self.records = {
            key: value for key, value in self.records.items() if key[0] != tenant or key[1] in named
        }


def _slack_error(code: str) -> SlackApiError:
    return SlackApiError(
        code,
        SlackResponse(
            client=None,
            http_verb="POST",
            api_url=f"{API_BASE_URL}usergroups.users.list",
            req_args={},
            data={"ok": False, "error": code},
            headers={},
            status_code=200,
        ),
    )


class _WebClient:
    def __init__(self, members: dict[str, list[str] | Exception]) -> None:
        self.members = members
        self.built: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    def __call__(self, token: str, base_url: str, **kwargs: Any) -> _WebClient:
        self.built.append({"token": token, "base_url": base_url, **kwargs})
        return self

    def usergroups_users_list(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        members = self.members[kwargs["usergroup"]]
        if isinstance(members, Exception):
            raise members
        return {"ok": True, "users": members}


def _refresh(config, store: _Store, web_client: Any = None, service: Any = None):
    reader = SlackGroupReader(
        hermetic_settings(slack_bot_token="xoxb-ADP-SECRET"),
        web_client=web_client or _WebClient({}),
        read_through_service=service or (lambda *args, **kwargs: []),
    )
    return ApproverGroupRefresh(
        tenant="adp",
        escalation=lambda: config,
        read=reader,
        store=store,
        clock=lambda: READ_AT,
    )


async def test_a_read_through_the_tenants_own_token_writes_the_members():
    web = _WebClient({GROUP: ["U0ONE", "U0TWO"]})
    store = _Store()

    await _refresh(_config(GROUP), store, web_client=web).refresh()

    assert store.records == {("adp", GROUP): (["U0ONE", "U0TWO"], READ_AT)}
    [built] = web.built
    assert (built["token"], built["base_url"], built["retry_handlers"]) == (
        "xoxb-ADP-SECRET",
        API_BASE_URL,
        [],
    )
    assert web.reads == [{"usergroup": GROUP}]


async def test_a_read_through_the_tenants_own_token_sends_the_team_id_the_entry_sets():
    web = _WebClient({GROUP: ["U0ONE"]})

    await _refresh(_config(GROUP, team_id="T0WORKSPACE"), _Store(), web_client=web).refresh()

    assert web.reads == [{"usergroup": GROUP, "team_id": "T0WORKSPACE"}]


async def test_a_read_through_the_posting_service_writes_the_members():
    calls: list[tuple[str, float]] = []

    def service(settings, group_id, *, timeout):
        calls.append((group_id, timeout))
        return ["U0SHARED"]

    store = _Store()

    await _refresh(_config(GROUP, app=SHARED_APP), store, service=service).refresh()

    assert store.records == {("adp", GROUP): (["U0SHARED"], READ_AT)}
    assert [group_id for group_id, _ in calls] == [GROUP]


async def test_a_failed_read_keeps_the_last_record_and_the_other_groups_are_read(caplog):
    web = _WebClient({GROUP: _slack_error("no_such_subteam"), OTHER_GROUP: ["U0OTHER"]})
    store = _Store({("adp", GROUP): (["U0ONE"], EARLIER)})

    with caplog.at_level(logging.ERROR):
        await _refresh(_config(GROUP, OTHER_GROUP), store, web_client=web).refresh()

    assert store.records == {
        ("adp", GROUP): (["U0ONE"], EARLIER),
        ("adp", OTHER_GROUP): (["U0OTHER"], READ_AT),
    }
    [message] = [record.getMessage() for record in caplog.records]
    assert GROUP in message and "adp" in message and "no_such_subteam" in message


async def test_a_refused_posting_service_read_keeps_the_last_record():
    def service(settings, group_id, *, timeout):
        raise PostingFailed("refused", error="group_not_named")

    store = _Store({("adp", GROUP): (["U0ONE"], EARLIER)})

    await _refresh(_config(GROUP, app=SHARED_APP), store, service=service).refresh()

    assert store.records == {("adp", GROUP): (["U0ONE"], EARLIER)}


@pytest.mark.parametrize("app", [OWN_APP, SHARED_APP])
async def test_missing_scope_logs_the_scope_and_the_reinstall_remedy(caplog, app):
    def service(settings, group_id, *, timeout):
        raise PostingFailed("refused", error="missing_scope")

    web = _WebClient({GROUP: _slack_error("missing_scope")})

    with caplog.at_level(logging.ERROR):
        await _refresh(_config(GROUP, app=app), _Store(), web_client=web, service=service).refresh()

    [message] = [record.getMessage() for record in caplog.records]
    assert "missing_scope" in message
    assert "usergroups:read" in message
    assert "reinstall the app" in message


@pytest.mark.parametrize("error", ["missing_argument", "team_not_found", "team_access_not_granted"])
async def test_a_team_refusal_logs_that_the_group_entry_must_set_team_id(caplog, error):
    web = _WebClient({GROUP: _slack_error(error)})

    with caplog.at_level(logging.ERROR):
        await _refresh(_config(GROUP), _Store(), web_client=web).refresh()

    [message] = [record.getMessage() for record in caplog.records]
    assert error in message
    assert f"must set team_id on the entry of group {GROUP}" in message
    assert "workspace that owns the group" in message


async def test_a_removed_groups_record_is_deleted_for_the_workers_tenant_only():
    web = _WebClient({OTHER_GROUP: ["U0OTHER"]})
    store = _Store(
        {
            ("adp", GROUP): (["U0ONE"], EARLIER),
            ("beta", GROUP): (["U0BETA"], EARLIER),
        }
    )

    await _refresh(_config(OTHER_GROUP), store, web_client=web).refresh()

    assert store.records == {
        ("adp", OTHER_GROUP): (["U0OTHER"], READ_AT),
        ("beta", GROUP): (["U0BETA"], EARLIER),
    }


async def test_a_tenant_without_slack_reads_nothing_and_clears_its_records_once():
    web = _WebClient({})
    store = _Store({("adp", GROUP): (["U0ONE"], EARLIER)})
    refresh = _refresh(_config(GROUP, channels=(EMAIL,)), store, web_client=web)

    await refresh.refresh()
    await refresh.refresh()

    assert store.records == {}
    assert store.forgets == 1
    assert web.reads == []


async def test_the_refresh_loop_stops_when_the_worker_cancels_it():
    reads = asyncio.Event()

    class _Refresh:
        async def refresh(self) -> None:
            reads.set()

    task = asyncio.create_task(keep_approver_groups(_Refresh(), interval=3600))
    await asyncio.wait_for(reads.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


class _Transaction:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)


class _Engine:
    def __init__(self) -> None:
        self.transaction = _Transaction()

    def begin(self):
        return self.transaction


def _compiled(statement) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


async def test_the_postgres_record_upserts_the_members_and_read_time_of_the_tenants_group():
    engine = _Engine()

    await PostgresApproverGroupStore(engine).record("adp", GROUP, ["U0ONE"], READ_AT)

    [statement] = engine.transaction.statements
    sql, params = _compiled(statement)
    assert "ON CONFLICT (tenant, group_id) DO UPDATE" in sql
    assert (params["tenant"], params["group_id"]) == ("adp", GROUP)
    assert (params["members"], params["refreshed_at"]) == (["U0ONE"], READ_AT)


async def test_the_postgres_forget_deletes_only_the_tenants_unnamed_groups():
    engine = _Engine()

    await PostgresApproverGroupStore(engine).forget_unnamed("adp", [GROUP])

    [statement] = engine.transaction.statements
    sql, params = _compiled(statement)
    assert sql.startswith("DELETE FROM agent_platform.slack_approver_groups")
    assert "NOT IN" in sql
    assert sorted(params.values(), key=str) == [[GROUP], "adp"]


ADMIN_SCOPE_DESTINATION = AdminDestination(
    slack_channel="C0ADMIN",
    allowlist={
        "repair": Approvers(groups=(SlackGroup(ORGANIZATION, GROUP, ""),)),
        "budget": Approvers(groups=(SlackGroup(ORGANIZATION, OTHER_GROUP, ""),)),
    },
)


def _admin_refresh(
    store: _Store,
    answers: dict[str, Any],
    destination=ADMIN_SCOPE_DESTINATION,
    *,
    lock=None,
    reads: list[str] | None = None,
):
    reads = [] if reads is None else reads

    def read_through_service(settings, group_id, *, timeout):
        reads.append(group_id)
        answer = answers[group_id]
        if isinstance(answer, Exception):
            raise answer
        return answer

    refresh = AdminGroupRefresh(
        destination=lambda: destination,
        read=PlatformGroupReader(hermetic_settings(), read_through_service=read_through_service),
        store=store,
        lock=lock,
        clock=lambda: READ_AT,
    )
    return refresh, reads


class _AdvisoryLocks:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.held: set[int] = set()
        self.refused = 0

    @contextlib.asynccontextmanager
    async def begin(self):
        taken: set[int] = set()
        locks = self

        class _Connection:
            async def scalar(self, statement, parameters=None):
                if "pg_try_advisory_xact_lock" in str(statement):
                    key = parameters["key"]
                    if key in locks.held:
                        locks.refused += 1
                        return False
                    locks.held.add(key)
                    taken.add(key)
                    return True
                scope = _compiled(statement)[1]["tenant_1"]
                times = [
                    at for (owner, _), (_, at) in locks.store.records.items() if owner == scope
                ]
                return max(times, default=None)

        try:
            yield _Connection()
        finally:
            locks.held -= taken


async def test_the_admin_refresh_records_the_admin_groups_under_the_admin_scope():
    store = _Store({("adp", GROUP): (["U0TENANT"], EARLIER)})
    refresh, reads = _admin_refresh(store, {GROUP: ["U0ADMIN"], OTHER_GROUP: ["U0OTHER"]})

    await refresh.refresh()

    assert sorted(reads) == [GROUP, OTHER_GROUP]
    assert store.records == {
        ("adp", GROUP): (["U0TENANT"], EARLIER),
        (ADMIN_GROUP_SCOPE, GROUP): (["U0ADMIN"], READ_AT),
        (ADMIN_GROUP_SCOPE, OTHER_GROUP): (["U0OTHER"], READ_AT),
    }


async def test_a_failed_admin_read_keeps_the_last_record_and_logs_the_slack_error(caplog):
    store = _Store({(ADMIN_GROUP_SCOPE, GROUP): (["U0ADMIN"], EARLIER)})
    refused = PostingFailed("the posting service refused the read", error="missing_scope")
    refresh, _ = _admin_refresh(store, {GROUP: refused, OTHER_GROUP: ["U0OTHER"]})

    with caplog.at_level(logging.ERROR):
        await refresh.refresh()

    assert store.records[(ADMIN_GROUP_SCOPE, GROUP)] == (["U0ADMIN"], EARLIER)
    assert store.records[(ADMIN_GROUP_SCOPE, OTHER_GROUP)] == (["U0OTHER"], READ_AT)
    [message] = [record.getMessage() for record in caplog.records]
    assert (
        GROUP in message and "platform administrators" in message and "usergroups:read" in message
    )


async def test_an_admin_group_no_longer_named_is_deleted_from_the_admin_scope_only():
    store = _Store(
        {
            (ADMIN_GROUP_SCOPE, GROUP): (["U0ADMIN"], EARLIER),
            (ADMIN_GROUP_SCOPE, OTHER_GROUP): (["U0OTHER"], EARLIER),
            ("adp", OTHER_GROUP): (["U0TENANT"], EARLIER),
        }
    )
    destination = AdminDestination(
        slack_channel="C0ADMIN",
        allowlist={"repair": Approvers(groups=(SlackGroup(ORGANIZATION, GROUP, ""),))},
    )
    refresh, reads = _admin_refresh(store, {GROUP: ["U0ADMIN"]}, destination)

    await refresh.refresh()

    assert reads == [GROUP]
    assert set(store.records) == {(ADMIN_GROUP_SCOPE, GROUP), ("adp", OTHER_GROUP)}


def test_the_webhook_runs_no_admin_refresh_without_an_admin_slack_channel(monkeypatch):
    monkeypatch.setattr(
        approver_groups, "process_admin_destination", lambda: AdminDestination(emails=("a@x.io",))
    )

    assert approver_groups.admin_group_refresh(hermetic_settings()) is None


async def test_two_admin_refresh_runs_at_once_perform_one_read_cycle():
    store = _Store({})
    engine = _AdvisoryLocks(store)
    now = [READ_AT]
    lock = PostgresRefreshLock(
        engine,
        key=approver_groups.ADMIN_REFRESH_LOCK,
        scope=ADMIN_GROUP_SCOPE,
        fresh_seconds=approver_groups.ADMIN_REFRESH_FRESH_SECONDS,
        clock=lambda: now[0],
    )
    answers = {GROUP: ["U0ADMIN"], OTHER_GROUP: ["U0OTHER"]}
    reads: list[str] = []
    first, _ = _admin_refresh(store, answers, lock=lock, reads=reads)
    second, _ = _admin_refresh(store, answers, lock=lock, reads=reads)

    await asyncio.gather(first.refresh(), second.refresh())
    concurrent = sorted(reads)
    await second.refresh()
    out_of_phase = sorted(reads)
    now[0] = READ_AT + timedelta(seconds=approver_groups.ADMIN_REFRESH_FRESH_SECONDS)
    await second.refresh()

    assert concurrent == out_of_phase == [GROUP, OTHER_GROUP]
    assert sorted(reads) == [GROUP, GROUP, OTHER_GROUP, OTHER_GROUP]
    assert engine.refused == 1
    assert engine.held == set()


def test_the_webhook_builds_the_admin_group_refresh_on_its_event_loop(monkeypatch):
    import asyncio

    from services.integration.slack import approver_groups
    from services.orchestration.platform_escalation import AdminDestination
    from services.orchestration.webhook import server
    from tests.support import hermetic_settings

    started: list[object] = []

    async def _keep(refresh, interval=0):
        started.append(refresh)

    monkeypatch.setattr(
        approver_groups,
        "process_admin_destination",
        lambda: AdminDestination(slack_channel="C0123456789"),
    )
    monkeypatch.setattr(server, "keep_approver_groups", _keep)
    settings = hermetic_settings().model_copy(
        update={"database_url": "postgresql+asyncpg://platform:unused@127.0.0.1:1/unused"}
    )

    asyncio.run(server.keep_admin_groups(settings))

    assert [type(refresh).__name__ for refresh in started] == ["AdminGroupRefresh"]
