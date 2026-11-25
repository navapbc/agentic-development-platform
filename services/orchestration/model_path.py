"""Health of the model path a run calls Claude through, from the worker to LiteLLM to Bedrock.

A model call crosses three hops, each named by a stable token.

- ``credentials``, the AWS session LiteLLM signs Bedrock requests with.
- ``bedrock_path``, LiteLLM's connection to the Bedrock runtime endpoint, which a workstation
  reaches through the connectivity tunnel.
- ``litellm``, the worker's connection to the LiteLLM gateway.

A model call that fails on one of these hops raises ``ApplicationError`` with type
``ModelUnavailable`` and ``non_retryable=True``. Its first detail is a mapping whose ``hop`` key
holds the hop token and whose ``detail`` key holds the gateway's answer or the transport error.
``model_unavailable_hop`` reads the hop back from the error or from an activity error that wraps
it. Workflows and the intake gate depend on the type name, the hop tokens, and the detail shape.

Intake probes the path with a one-token completion before it starts a run. While the path is
unhealthy the gate defers every candidate, and one alert per transition reaches the tenant's
escalation channels, deduplicated across passes and worker restarts by the episode row and the
side-effect ledger. A run whose model call fails this way parks as ``model_unavailable``, and
the first fresh probe that finds the path healthy resumes the tenant's parked runs.

The episode keeps the Slack channel and ``ts`` of its outage post. A recovery, whether intake
finds it or a Fix done click confirms it, edits that post to resolved and replies in its thread.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import structlog
from temporalio.exceptions import ApplicationError

from services.orchestration.model_path_parks import resume_each
from services.orchestration.remedies import MODEL_PATH_FAILURES

if TYPE_CHECKING:
    from services.coordination.idempotency import SideEffectLedger, SideEffectRef

log = structlog.get_logger("model_path")

MODEL_UNAVAILABLE = "ModelUnavailable"
HOP_CREDENTIALS = "credentials"
HOP_BEDROCK_PATH = "bedrock_path"
HOP_LITELLM = "litellm"
HOPS = (HOP_CREDENTIALS, HOP_BEDROCK_PATH, HOP_LITELLM)

PROBE_TIMEOUT_SECONDS = 30.0
PROBE_CACHE_SECONDS = 50.0
_DETAIL_LIMIT = 500

_CREDENTIAL_MARKERS = (
    "refreshed credentials are still expired",
    "expiredtoken",
    "unrecognizedclientexception",
    "invalidclienttokenid",
    "security token included in the request is expired",
    "security token included in the request is invalid",
    "unable to locate credentials",
    "error when retrieving credentials from custom-process",
    "token has expired and refresh failed",
)
_CONNECTION_MARKERS = (
    "cannot connect to host",
    "could not connect to the endpoint url",
    "connect timeout on endpoint url",
    "connection refused",
    "connecterror",
    "connecttimeout",
    "name or service not known",
    "nodename nor servname provided",
    "temporary failure in name resolution",
)


def classify_gateway_answer(body: str) -> str | None:
    """Name the hop a failed LiteLLM answer points at, or None for any other failure.

    LiteLLM wraps a Bedrock failure in a 4xx or 5xx answer whose text keeps botocore's or the
    HTTP client's message, so the hop is read from the text whatever the status.
    """
    text = body.lower()
    if any(marker in text for marker in _CREDENTIAL_MARKERS):
        return HOP_CREDENTIALS
    if "bedrock" in text and any(marker in text for marker in _CONNECTION_MARKERS):
        return HOP_BEDROCK_PATH
    return None


def classify_transport_error(exc: httpx.TransportError) -> str | None:
    """Name the hop of a request that could not open a connection to LiteLLM.

    A read timeout is not classified, because a long completion can outlast the read timeout
    while the gateway is healthy.
    """
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return HOP_LITELLM
    return None


def model_unavailable_error(hop: str, detail: str) -> ApplicationError:
    failure = MODEL_PATH_FAILURES.get(hop, "a hop of the model path failed")
    bounded = detail[:_DETAIL_LIMIT]
    return ApplicationError(
        f"the model path is unavailable at the {hop} hop, because {failure}. {bounded}",
        {"hop": hop, "detail": bounded},
        type=MODEL_UNAVAILABLE,
        non_retryable=True,
    )


def model_unavailable_hop(error: BaseException | None) -> str | None:
    """Return the hop of a ModelUnavailable failure, following the activity error's causes."""
    while error is not None:
        if isinstance(error, ApplicationError) and error.type == MODEL_UNAVAILABLE:
            details = error.details[0] if error.details else {}
            hop = details.get("hop") if isinstance(details, dict) else None
            return str(hop) if hop else None
        error = error.__cause__ or getattr(error, "cause", None)
    return None


@dataclass(frozen=True)
class ModelPathHealth:
    hop: str = ""
    error: str = ""

    @property
    def healthy(self) -> bool:
        return not self.hop


async def probe_model_path(
    settings: Any, *, transport: httpx.AsyncBaseTransport | None = None
) -> ModelPathHealth:
    """Send a one-token completion to LiteLLM with the request shape and model a run uses.

    An answer that names no hop, such as a throttled request, reports the path healthy, so
    intake defers only on a failure it can name a remedy for. A host-run pass, such as
    ``inv intake --local``, reaches LiteLLM on its published port.
    """
    from scripts.config import to_host_url

    from services.orchestration.temporal_activities.bedrock import _litellm_auth_headers
    from services.orchestration.temporal_workflows.review import PLANNING_MODEL

    payload = {
        "model": PLANNING_MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    base_url = to_host_url(settings.litellm_base_url, settings)
    try:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=PROBE_TIMEOUT_SECONDS, transport=transport
        ) as client:
            resp = await client.post(
                "/v1/chat/completions", json=payload, headers=_litellm_auth_headers(settings)
            )
    except httpx.TransportError as exc:
        hop = classify_transport_error(exc) or HOP_LITELLM
        detail = f"{base_url} {exc.__class__.__name__} {exc}"
        return ModelPathHealth(hop=hop, error=detail[:_DETAIL_LIMIT])
    if resp.status_code < 400:
        return ModelPathHealth()
    detail = f"litellm returned {resp.status_code} with {resp.text[:_DETAIL_LIMIT]}"
    hop = classify_gateway_answer(resp.text)
    if hop is None:
        log.warning("model_path_probe_unclassified", status=resp.status_code, detail=detail)
        return ModelPathHealth(error=detail)
    return ModelPathHealth(hop=hop, error=detail)


class ModelPathProbe:
    """Cache one probe answer in process for ``ttl`` seconds and serialize concurrent probes."""

    def __init__(
        self,
        probe: Callable[[], Awaitable[ModelPathHealth]],
        *,
        ttl: float = PROBE_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._answer: ModelPathHealth | None = None
        self._at = 0.0

    async def check(self) -> tuple[ModelPathHealth, bool]:
        """Return the path's health and whether this call sent the probe."""
        async with self._lock:
            if self._answer is not None and self._clock() - self._at < self._ttl:
                return self._answer, False
            answer = await self._probe()
            self._answer, self._at = answer, self._clock()
            return answer, True

    async def refresh(self) -> ModelPathHealth:
        """Send a probe whatever the cache holds, and cache its answer."""
        async with self._lock:
            answer = await self._probe()
            self._answer, self._at = answer, self._clock()
            return answer


_SHARED_PROBES: dict[tuple[str, str], ModelPathProbe] = {}


def shared_probe(settings: Any) -> ModelPathProbe:
    """Return the process's probe for the tenant and gateway, shared by every intake path."""
    key = (settings.tenant.strip(), settings.litellm_base_url)
    probe = _SHARED_PROBES.get(key)
    if probe is None:

        async def _probe() -> ModelPathHealth:
            return await probe_model_path(settings)

        probe = _SHARED_PROBES[key] = ModelPathProbe(_probe)
    return probe


@dataclass(frozen=True)
class ModelPathEpisode:
    episode: str
    hop: str
    since: datetime | None = None
    slack_channel: str = ""
    slack_ts: str = ""
    acknowledged_by: str = ""
    acknowledged_at: datetime | None = None
    admin_slack_channel: str = ""
    admin_slack_ts: str = ""
    admin_slack_token: str = ""
    slack_blocks: str = ""
    admin_slack_blocks: str = ""
    """The JSON of the blocks each post was sent with, which its later edits keep."""
    host_action: str = ""
    host_action_at: datetime | None = None
    host_action_posted_at: datetime | None = None

    @property
    def host_action_pending(self) -> bool:
        return bool(self.host_action) and self.host_action_at != self.host_action_posted_at


@dataclass(frozen=True)
class OutagePost:
    channel: str
    ts: str
    admin: bool = False
    owner_token: str = ""
    blocks: str = ""


class ModelPathEpisodes(Protocol):
    async def open(self, tenant: str, hop: str) -> ModelPathEpisode: ...

    async def current(self, tenant: str) -> ModelPathEpisode | None: ...

    async def close(self, tenant: str, episode: str, *, resolved_by: str = "") -> None: ...

    async def record_post(self, tenant: str, episode: str, post: OutagePost) -> None: ...

    async def resolved(self, tenant: str, episode: str) -> ModelPathEpisode | None:
        """Return ``episode`` when it is closed and still the tenant's last episode."""
        ...

    async def acknowledge(
        self, tenant: str, episode: str, actor: str, at: datetime
    ) -> ModelPathEpisode | None:
        """Record the acknowledger of the open episode, or return None when it is not open."""
        ...

    async def mark_host_action_posted(self, tenant: str, episode: str, at: datetime) -> None:
        """Record that the host action written at ``at`` is posted, and leave a later one."""
        ...


class ModelPathAlerter(Protocol):
    async def unavailable(
        self, *, hop: str, detail: str, issue_key: str, episode: str = ""
    ) -> Sequence[OutagePost]:
        """Send the outage alert, and return the Slack posts it made."""
        ...

    async def recovered(
        self,
        *,
        hop: str,
        issue_key: str,
        episode: ModelPathEpisode | None = None,
        resolver: str = "",
    ) -> None: ...

    async def host_action(self, *, episode: ModelPathEpisode) -> None:
        """Post the episode's host action in the thread of each of its Slack posts."""
        ...


class ModelPathParks(Protocol):
    """The runs of a tenant parked because a model call found the model path unavailable."""

    async def parked(self, tenant: str) -> list[str]: ...

    async def resume(self, workflow_id: str) -> bool: ...


_UNHEALTHY = "unhealthy"
_HEALTHY = "healthy"


class PostgresModelPathEpisodes:
    """One row per tenant holding whether its model path is unhealthy and since when."""

    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from database.engine import shared_platform_engine

            self._engine = shared_platform_engine()
        return self._engine

    async def open(self, tenant: str, hop: str) -> ModelPathEpisode:
        from database.models.schema import model_path_health as table
        from sqlalchemy import case, func
        from sqlalchemy.dialects.postgresql import insert

        stmt = insert(table).values(
            tenant=tenant, state=_UNHEALTHY, hop=hop, episode=uuid.uuid4().hex
        )
        still_open = table.c.state == _UNHEALTHY
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.tenant],
            set_={
                "episode": case((still_open, table.c.episode), else_=stmt.excluded.episode),
                "since": case((still_open, table.c.since), else_=func.now()),
                "state": _UNHEALTHY,
                "hop": hop,
                "updated_at": func.now(),
                **{
                    column: case((still_open, table.c[column]), else_="")
                    for column in _EPISODE_TEXT_COLUMNS
                },
                **{
                    column: case((still_open, table.c[column]), else_=None)
                    for column in _EPISODE_TIME_COLUMNS
                },
            },
        ).returning(table.c.episode, table.c.hop, table.c.since)
        async with self._get_engine().begin() as conn:
            row = (await conn.execute(stmt)).one()
        return ModelPathEpisode(episode=row.episode, hop=row.hop, since=row.since)

    async def current(self, tenant: str) -> ModelPathEpisode | None:
        from database.models.schema import model_path_health as table
        from sqlalchemy import select

        stmt = select(*_episode_columns(table)).where(
            table.c.tenant == tenant, table.c.state == _UNHEALTHY
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        return None if row is None else _episode(row)

    async def close(self, tenant: str, episode: str, *, resolved_by: str = "") -> None:
        from sqlalchemy import func

        await self._update_open(
            tenant,
            episode,
            state=_HEALTHY,
            resolved_by=resolved_by[:128],
            resolved_at=func.now(),
            updated_at=func.now(),
        )

    async def record_post(self, tenant: str, episode: str, post: OutagePost) -> None:
        if post.admin:
            await self._update_open(
                tenant,
                episode,
                admin_slack_channel=post.channel,
                admin_slack_ts=post.ts,
                admin_slack_token=post.owner_token,
                admin_slack_blocks=post.blocks,
            )
        else:
            await self._update_open(
                tenant,
                episode,
                slack_channel=post.channel,
                slack_ts=post.ts,
                slack_blocks=post.blocks,
            )

    async def resolved(self, tenant: str, episode: str) -> ModelPathEpisode | None:
        from database.models.schema import model_path_health as table
        from sqlalchemy import select

        stmt = select(*_episode_columns(table)).where(
            table.c.tenant == tenant, table.c.episode == episode, table.c.state == _HEALTHY
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        return None if row is None else _episode(row)

    async def mark_host_action_posted(self, tenant: str, episode: str, at: datetime) -> None:
        from database.models.schema import model_path_health as table
        from sqlalchemy import update

        stmt = (
            update(table)
            .where(table.c.tenant == tenant, table.c.episode == episode)
            .where(table.c.host_action_at == at)
            .values(host_action_posted_at=at)
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)

    async def acknowledge(
        self, tenant: str, episode: str, actor: str, at: datetime
    ) -> ModelPathEpisode | None:
        row = await self._update_open(
            tenant, episode, acknowledged_by=actor[:128], acknowledged_at=at
        )
        return None if row is None else _episode(row)

    async def _update_open(self, tenant: str, episode: str, **values: Any) -> Any:
        from database.models.schema import model_path_health as table
        from sqlalchemy import update

        stmt = (
            update(table)
            .where(table.c.tenant == tenant, table.c.episode == episode)
            .where(table.c.state == _UNHEALTHY)
            .values(**values)
            .returning(*_episode_columns(table))
        )
        async with self._get_engine().begin() as conn:
            return (await conn.execute(stmt)).first()


_EPISODE_TEXT_COLUMNS = (
    "slack_channel",
    "slack_ts",
    "admin_slack_channel",
    "admin_slack_ts",
    "admin_slack_token",
    "slack_blocks",
    "admin_slack_blocks",
    "acknowledged_by",
    "resolved_by",
    "host_action",
)
_EPISODE_TIME_COLUMNS = (
    "acknowledged_at",
    "resolved_at",
    "host_action_at",
    "host_action_posted_at",
)
_EPISODE_FIELDS = (
    "slack_channel",
    "slack_ts",
    "admin_slack_channel",
    "admin_slack_ts",
    "admin_slack_token",
    "slack_blocks",
    "admin_slack_blocks",
    "acknowledged_by",
    "host_action",
)


def _episode_columns(table: Any) -> tuple[Any, ...]:
    return tuple(
        table.c[name]
        for name in (
            "episode",
            "hop",
            "since",
            *_EPISODE_FIELDS,
            "acknowledged_at",
            "host_action_at",
            "host_action_posted_at",
        )
    )


def _episode(row: Any) -> ModelPathEpisode:
    return ModelPathEpisode(
        episode=row.episode,
        hop=row.hop,
        since=row.since,
        **{name: getattr(row, name) or "" for name in _EPISODE_FIELDS},
        acknowledged_at=row.acknowledged_at,
        host_action_at=row.host_action_at,
        host_action_posted_at=row.host_action_posted_at,
    )


async def record_host_action(text: str, *, engine: Any) -> int:
    """Write ``text`` as the host action of every open episode that holds another, and return
    the number of episodes written.

    The host agent serves every tenant of the workstation, so it writes each open episode. An
    episode that already holds ``text`` keeps its time, so a report the agent repeats each pass
    posts once.
    """
    from database.models.schema import model_path_health as table
    from sqlalchemy import func, update

    text = text[:500]
    stmt = (
        update(table)
        .where(table.c.state == _UNHEALTHY, table.c.host_action != text)
        .values(host_action=text, host_action_at=func.now())
    )
    async with engine.begin() as conn:
        return (await conn.execute(stmt)).rowcount or 0


def _alert_ref(tenant: str, operation: str, episode: str) -> SideEffectRef:
    from services.coordination.idempotency import SideEffectRef

    return SideEffectRef(
        task_id=f"model-path@{tenant}", execution_id="", operation=operation, target=episode
    )


class ModelPathGate:
    """Probe the model path before intake starts a run, and alert once per health transition.

    Only a call that sent a fresh probe records a transition, so a pass that reuses the cached
    answer touches no table. The alert of an episode runs through the side-effect ledger before
    the episode closes, so a worker that stops between the two replays the alert rather than
    sending it again. Every fresh healthy answer resumes the tenant's runs parked on the model
    path, whether or not intake saw the outage that parked them. A host action the workstation's
    host agent recorded on the open episode posts in the episode's threads, and the path is probed
    past the cache, so the fix the agent made resolves the alert in the same pass.
    """

    def __init__(
        self,
        *,
        probe: ModelPathProbe,
        episodes: ModelPathEpisodes,
        alerter: ModelPathAlerter,
        ledger: SideEffectLedger,
        tenant: str,
        parks: ModelPathParks | None = None,
    ) -> None:
        self._probe = probe
        self._episodes = episodes
        self._alerter = alerter
        self._ledger = ledger
        self._tenant = tenant
        self._parks = parks

    async def admit(self, *, issue_key: str) -> ModelPathHealth:
        health, fresh = await self._probe.check()
        if fresh:
            if health.healthy:
                await self._record_recovery(issue_key)
            else:
                await self._record_outage(health, issue_key)
        return health

    async def _record_outage(self, health: ModelPathHealth, issue_key: str) -> None:
        episode = await self._episodes.open(self._tenant, health.hop)

        async def _send() -> dict[str, str]:
            posts = await self._alerter.unavailable(
                hop=health.hop, detail=health.error, issue_key=issue_key, episode=episode.episode
            )
            for post in posts or ():
                await self._episodes.record_post(self._tenant, episode.episode, post)
            return {"hop": health.hop, "issue_key": issue_key}

        ref = _alert_ref(self._tenant, "model_path_unavailable_alert", episode.episode)
        outcome = await self._ledger.run_once(ref, _send)
        if not (outcome.replayed or outcome.in_flight):
            log.warning(
                "model_path_unavailable", tenant=self._tenant, hop=health.hop, detail=health.error
            )

    async def sweep(self) -> None:
        """Probe the path for the tenant's parked runs, which a pass with no pickup never does.

        A pass that already probed reuses that answer, which recorded its own transition. A pass
        that posted a host action probes past the cache, parked runs or not.
        """
        if await self._report_host_action():
            health, fresh = await self._probe.refresh(), True
        elif await self._parked():
            health, fresh = await self._probe.check()
        else:
            return
        if not fresh:
            return
        if health.healthy:
            await self._record_recovery("")
        else:
            await self._record_outage(health, "")

    async def confirm_fix(self, *, resolver: str) -> ModelPathHealth:
        """Probe the path past the cache, and on a healthy answer resolve the episode as
        ``resolver`` and resume the tenant's parked runs."""
        health = await self._probe.refresh()
        if health.healthy:
            await self._record_recovery("", resolver=resolver)
        else:
            await self._record_outage(health, "")
        return health

    async def _report_host_action(self) -> bool:
        try:
            episode = await self._episodes.current(self._tenant)
        except Exception:
            log.warning("model_path_episode_unreadable", tenant=self._tenant, exc_info=True)
            return False
        if episode is None or not episode.host_action_pending or episode.host_action_at is None:
            return False
        try:
            await self._alerter.host_action(episode=episode)
        except Exception:
            log.warning("model_path_host_action_unposted", tenant=self._tenant, exc_info=True)
            return False
        await self._episodes.mark_host_action_posted(
            self._tenant, episode.episode, episode.host_action_at
        )
        log.info("model_path_host_action_posted", tenant=self._tenant, action=episode.host_action)
        return True

    async def _record_recovery(self, issue_key: str, *, resolver: str = "") -> None:
        episode = await self._episodes.current(self._tenant)
        if episode is not None:

            async def _send() -> dict[str, str]:
                await self._alerter.recovered(
                    hop=episode.hop, issue_key=issue_key, episode=episode, resolver=resolver
                )
                return {"hop": episode.hop, "issue_key": issue_key}

            ref = _alert_ref(self._tenant, "model_path_recovered_alert", episode.episode)
            await self._ledger.run_once(ref, _send)
            await self._episodes.close(self._tenant, episode.episode, resolved_by=resolver)
            log.info("model_path_recovered", tenant=self._tenant, hop=episode.hop)
        await self._resume_parks()

    async def _parked(self) -> list[str]:
        if self._parks is None:
            return []
        try:
            return await self._parks.parked(self._tenant)
        except Exception:
            log.warning("model_path_parks_unreadable", tenant=self._tenant, exc_info=True)
            return []

    async def _resume_parks(self) -> list[str]:
        parks = self._parks
        if parks is None:
            return []

        async def _resume(workflow_id: str) -> bool:
            try:
                return bool(await parks.resume(workflow_id))
            except Exception:
                log.warning(
                    "model_path_park_resume_failed",
                    tenant=self._tenant,
                    workflow_id=workflow_id,
                    exc_info=True,
                )
                return False

        resumed = await resume_each(await self._parked(), _resume)
        if resumed:
            log.info("model_path_parks_resumed", tenant=self._tenant, workflow_ids=resumed)
        return resumed


def model_path_deferral_reason(health: ModelPathHealth) -> str:
    return f"model_unavailable: the {health.hop} hop of the model path failed"
