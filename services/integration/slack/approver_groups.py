"""Refresh of the members of the Slack user groups a tenant names as approvers.

The tenant's activity worker reads every group that its current escalation.yaml names under a
decision kind, at start and every GROUP_REFRESH_SECONDS, and records the members with the read
time in slack_approver_groups for its own tenant, where every dispatcher of the tenant reads them.
A tenant on its own app reads with usergroups.users.list, its own bot token, and its api_base_url.
A tenant on the shared app asks the Slack posting service, which holds the shared bot token.

A read passes team_id only when the group's entry in escalation.yaml sets it. Slack requires
team_id, the workspace that owns the group, when the token is an org-level token of an Enterprise
Grid install, and ignores it for a workspace token. When Slack refuses a read for a missing or
unknown team, the log says to set team_id on the group's entry.

A failed read leaves the group's record unchanged, so the dispatcher refuses the group's members
as stale once the record is older than its stale limit. A refresh deletes the records of groups
that the tenant no longer names, and reads Slack only while the tenant's escalation.yaml enables
Slack and names at least one group.

The inbound webhook runs the same refresh for the groups the allowlist of platform-escalation.yaml
names, recorded under ADMIN_GROUP_SCOPE, where the dispatcher reads them for an administrator click.
It reads them through the posting service as the platform caller, and runs only while
platform-escalation.yaml names an administrator Slack channel. Every webhook replica runs the
refresh, and a cycle runs in one replica only, the one that takes the transaction advisory lock
ADMIN_REFRESH_LOCK while no replica recorded an administrator group in the last
ADMIN_REFRESH_FRESH_SECONDS. Every other replica skips the cycle, so replicas out of phase read
Slack once per cycle between them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from scripts.config import Settings
from slack_sdk.errors import SlackApiError

from services.integration.credentials import ConfigError
from services.integration.slack.client import tenant_web_client
from services.integration.slack.poster_client import PostingFailed, read_group_members
from services.integration.slack.shared_app import process_shared_app
from services.orchestration.escalation import (
    SHARED_APP,
    SLACK,
    EscalationConfig,
    SlackGroup,
    SlackSettings,
    load_escalation_config,
)
from services.orchestration.platform_escalation import (
    ADMIN_GROUP_SCOPE,
    AdminDestination,
    process_admin_destination,
)

log = logging.getLogger(__name__)

GROUP_REFRESH_SECONDS = 300
GROUP_READ_TIMEOUT_SECONDS = 10
GROUP_READ_SCOPE = "usergroups:read"
MISSING_SCOPE = "missing_scope"
TEAM_ERRORS = frozenset({"missing_argument", "team_not_found", "team_access_not_granted"})
ADMIN_REFRESH_LOCK = 0x41445047524F5550
ADMIN_REFRESH_FRESH_SECONDS = GROUP_REFRESH_SECONDS / 2


def group_read_arguments(group: SlackGroup) -> dict[str, str]:
    """Return the usergroups.users.list arguments of a group, with team_id only when it is set."""
    arguments = {"usergroup": group.group_id}
    if group.team_id:
        arguments["team_id"] = group.team_id
    return arguments


class GroupReadFailed(Exception):
    """Slack, or the posting service, did not answer the members of a group.

    ``error`` holds the Slack error code, or a short reason when no code came back.
    """

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


GroupReader = Callable[[SlackSettings, SlackGroup], Sequence[str]]


class ApproverGroupStore(Protocol):
    async def record(
        self, tenant: str, group_id: str, members: Sequence[str], refreshed_at: datetime
    ) -> None: ...

    async def forget_unnamed(self, tenant: str, named: Sequence[str]) -> None: ...


class PostgresApproverGroupStore:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def record(
        self, tenant: str, group_id: str, members: Sequence[str], refreshed_at: datetime
    ) -> None:
        from database.models.schema import slack_approver_groups as groups
        from sqlalchemy.dialects.postgresql import insert

        values = {"members": list(members), "refreshed_at": refreshed_at}
        stmt = (
            insert(groups)
            .values(tenant=tenant, group_id=group_id, **values)
            .on_conflict_do_update(index_elements=[groups.c.tenant, groups.c.group_id], set_=values)
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def forget_unnamed(self, tenant: str, named: Sequence[str]) -> None:
        from database.models.schema import slack_approver_groups as groups
        from sqlalchemy import delete

        stmt = delete(groups).where(groups.c.tenant == tenant)
        if named:
            stmt = stmt.where(groups.c.group_id.not_in(list(named)))
        async with self._engine.begin() as conn:
            await conn.execute(stmt)


class SlackGroupReader:
    def __init__(
        self,
        settings: Settings,
        *,
        web_client: Callable[..., Any] = tenant_web_client,
        read_through_service: Callable[..., list[str]] = read_group_members,
    ) -> None:
        self._settings = settings
        self._web_client = web_client
        self._read_through_service = read_through_service

    def __call__(self, slack: SlackSettings, group: SlackGroup) -> list[str]:
        if slack.app == SHARED_APP:
            return self._through_service(group)
        return self._with_own_token(slack, group)

    def _through_service(self, group: SlackGroup) -> list[str]:
        try:
            return self._read_through_service(
                self._settings, group.group_id, timeout=GROUP_READ_TIMEOUT_SECONDS
            )
        except PostingFailed as exc:
            raise GroupReadFailed(exc.error or str(exc)) from None

    def _with_own_token(self, slack: SlackSettings, group: SlackGroup) -> list[str]:
        if not self._settings.slack_bot_token:
            raise GroupReadFailed("SLACK_BOT_TOKEN is not set")
        client = self._web_client(
            self._settings.slack_bot_token,
            slack.api_base_url,
            timeout=GROUP_READ_TIMEOUT_SECONDS,
            retry_handlers=[],
        )
        try:
            response = client.usergroups_users_list(**group_read_arguments(group))
        except SlackApiError as exc:
            raise GroupReadFailed(str(exc.response.get("error") or "unknown_error")) from None
        members = response.get("users")
        if not isinstance(members, list) or not all(isinstance(user, str) for user in members):
            raise GroupReadFailed("no_users")
        return members


def named_groups(config: EscalationConfig) -> tuple[SlackGroup, ...]:
    """Return each group the config names under any decision kind, once per group ID.

    A config that does not enable Slack names no group.
    """
    slack = config.slack
    if slack is None or SLACK not in config.channels:
        return ()
    groups: dict[str, SlackGroup] = {}
    for approvers in slack.allowlist.values():
        for group in approvers.groups:
            groups.setdefault(group.group_id, group)
    return tuple(groups.values())


def admin_groups(destination: AdminDestination | None) -> tuple[SlackGroup, ...]:
    """Return each group the administrator allowlist names, once per group ID.

    A destination that names no administrator Slack channel names no group.
    """
    if destination is None or not destination.slack:
        return ()
    groups: dict[str, SlackGroup] = {}
    for approvers in destination.allowlist.values():
        for group in approvers.groups:
            groups.setdefault(group.group_id, group)
    return tuple(groups.values())


class _GroupRecords:
    def __init__(
        self, *, scope: str, owner: str, store: ApproverGroupStore, clock: Callable[[], datetime]
    ) -> None:
        self._tenant = scope
        self._owner = owner
        self._store = store
        self._clock = clock

    async def _record_group(self, group: SlackGroup, read: Callable[[], Sequence[str]]) -> None:
        try:
            members = await asyncio.to_thread(read)
            await self._store.record(self._tenant, group.group_id, members, self._clock())
        except GroupReadFailed as exc:
            self._log_failure(group, exc.error)
        except Exception as exc:  # noqa: BLE001
            self._log_failure(group, type(exc).__name__)

    def _log_failure(self, group: SlackGroup, error: str) -> None:
        remedy = ""
        if error == MISSING_SCOPE:
            remedy = (
                f". The Slack app lacks the {GROUP_READ_SCOPE} scope. Add {GROUP_READ_SCOPE} to "
                "the app's bot token scopes and reinstall the app"
            )
        elif error in TEAM_ERRORS:
            remedy = (
                f". An org-level install must set team_id on the entry of group {group.group_id} "
                "in slack.allowlist to the ID of the workspace that owns the group"
            )
        log.error(
            "Slack user group %s of %s was not read, so its last record is kept. Slack error %s%s",
            group.group_id,
            self._owner,
            error,
            remedy,
        )


class ApproverGroupRefresh(_GroupRecords):
    """One tenant's refresh of the records of the groups its escalation.yaml names."""

    def __init__(
        self,
        *,
        tenant: str,
        escalation: Callable[[], EscalationConfig],
        read: GroupReader,
        store: ApproverGroupStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(scope=tenant, owner=f"tenant {tenant}", store=store, clock=clock)
        self._escalation = escalation
        self._read = read
        self._named_groups_last_refresh = True

    async def refresh(self) -> None:
        """Read every named group and delete the records of the groups no longer named.

        A config that cannot be read changes no record. A group whose read or write fails is
        logged and does not stop the reads of the other groups. While the config names no
        group, only the first refresh and the one after the last group was removed touch the
        database.
        """
        try:
            config = await asyncio.to_thread(self._escalation)
        except ConfigError as exc:
            log.error(
                "Slack approver groups of tenant %s were not refreshed, because escalation.yaml "
                "could not be read (%s)",
                self._tenant,
                exc,
            )
            return
        groups = named_groups(config)
        if groups or self._named_groups_last_refresh:
            await self._store.forget_unnamed(self._tenant, [group.group_id for group in groups])
        self._named_groups_last_refresh = bool(groups)
        if config.slack is None:
            return
        slack = config.slack
        for group in groups:
            await self._record_group(group, lambda group=group: self._read(slack, group))


class PlatformGroupReader:
    def __init__(
        self,
        settings: Settings,
        *,
        read_through_service: Callable[..., list[str]] = read_group_members,
    ) -> None:
        self._settings = settings
        self._read_through_service = read_through_service

    def __call__(self, group: SlackGroup) -> list[str]:
        try:
            return self._read_through_service(
                self._settings, group.group_id, timeout=GROUP_READ_TIMEOUT_SECONDS
            )
        except PostingFailed as exc:
            raise GroupReadFailed(exc.error or str(exc)) from None


class RefreshLock(Protocol):
    def cycle(self) -> AbstractAsyncContextManager[bool]: ...


class PostgresRefreshLock:
    """Admit one replica to a refresh cycle with a transaction advisory lock.

    The replica that takes the lock still skips the cycle when a group of ``scope`` was recorded
    within ``fresh_seconds``, because another replica ran that cycle.
    """

    def __init__(
        self,
        engine: Any,
        *,
        key: int,
        scope: str,
        fresh_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._key = key
        self._scope = scope
        self._fresh_seconds = fresh_seconds
        self._clock = clock

    @asynccontextmanager
    async def cycle(self) -> AsyncIterator[bool]:
        from database.models.schema import slack_approver_groups as groups
        from sqlalchemy import func, select, text

        async with self._engine.begin() as conn:
            taken = await conn.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": self._key}
            )
            if taken:
                last = await conn.scalar(
                    select(func.max(groups.c.refreshed_at)).where(groups.c.tenant == self._scope)
                )
                taken = last is None or (
                    (self._clock() - last).total_seconds() >= self._fresh_seconds
                )
            yield bool(taken)


class AdminGroupRefresh(_GroupRecords):
    def __init__(
        self,
        *,
        destination: Callable[[], AdminDestination | None],
        read: Callable[[SlackGroup], Sequence[str]],
        store: ApproverGroupStore,
        lock: RefreshLock | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            scope=ADMIN_GROUP_SCOPE, owner="the platform administrators", store=store, clock=clock
        )
        self._destination = destination
        self._read = read
        self._lock = lock

    async def refresh(self) -> None:
        """Read every administrator group and delete the records of the groups no longer named,
        unless another replica holds or ran this cycle."""
        if self._lock is None:
            await self._refresh()
            return
        async with self._lock.cycle() as admitted:
            if not admitted:
                log.debug("Slack administrator groups are refreshed by another replica this cycle")
                return
            await self._refresh()

    async def _refresh(self) -> None:
        groups = admin_groups(await asyncio.to_thread(self._destination))
        await self._store.forget_unnamed(ADMIN_GROUP_SCOPE, [group.group_id for group in groups])
        for group in groups:
            await self._record_group(group, lambda group=group: self._read(group))


class GroupRefresh(Protocol):
    async def refresh(self) -> None: ...


async def keep_approver_groups(
    refresh: GroupRefresh, interval: float = GROUP_REFRESH_SECONDS
) -> None:
    """Refresh at once and then every ``interval`` seconds until the task is cancelled."""
    while True:
        try:
            await refresh.refresh()
        except Exception as exc:  # noqa: BLE001
            log.error("Slack approver group refresh failed with %s", type(exc).__name__)
        await asyncio.sleep(interval)


def start_approver_group_refresh(settings: Settings) -> asyncio.Task[None]:
    from database.engine import shared_platform_engine

    shared_app = process_shared_app()
    refresh = ApproverGroupRefresh(
        tenant=settings.tenant,
        escalation=lambda: load_escalation_config(settings, shared_app=shared_app),
        read=SlackGroupReader(settings),
        store=PostgresApproverGroupStore(shared_platform_engine(settings.database_url)),
    )
    return asyncio.create_task(keep_approver_groups(refresh), name="slack-approver-groups")


def admin_group_refresh(settings: Settings) -> AdminGroupRefresh | None:
    """Return the inbound webhook's administrator group refresh, or None when
    platform-escalation.yaml names no administrator Slack channel."""
    from database.engine import shared_platform_engine

    destination = process_admin_destination()
    if destination is None or not destination.slack:
        log.info(
            "Slack administrator groups are not refreshed, because platform-escalation.yaml names "
            "no administrator Slack channel"
        )
        return None
    engine = shared_platform_engine(settings.database_url)
    return AdminGroupRefresh(
        destination=lambda: destination,
        read=PlatformGroupReader(settings),
        store=PostgresApproverGroupStore(engine),
        lock=PostgresRefreshLock(
            engine,
            key=ADMIN_REFRESH_LOCK,
            scope=ADMIN_GROUP_SCOPE,
            fresh_seconds=ADMIN_REFRESH_FRESH_SECONDS,
        ),
    )
