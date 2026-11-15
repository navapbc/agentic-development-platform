"""LiteLLM admin API client and the per-run virtual keys that sandbox and gate pods present.

A pod that makes model calls receives a virtual key scoped to its run, owned by the tenant's
LiteLLM team, capped at the run budget, and expiring after the access TTL. The master key stays
in the worker, which keeps using it for its own planning, review, and classification calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from observability.gateway import tenant_tag

RUN_MODEL_BUDGET_EXCEEDED = "run_model_budget_exceeded"
TENANT_MODEL_BUDGET_EXCEEDED = "tenant_model_budget_exceeded"
MODEL_BUDGET_REASONS = frozenset({RUN_MODEL_BUDGET_EXCEEDED, TENANT_MODEL_BUDGET_EXCEEDED})

_TIMEOUT_SECONDS = 15.0
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_ATTEMPTS = 3
_TEAM_CACHE_SECONDS = 300.0
# The proxy writes spend to its database in batches, every 10 seconds by default
# (general_settings.proxy_batch_write_at), so a refusal can precede the spend that caused it.
_SPEND_SETTLE_INTERVAL_SECONDS = 5.0
_SPEND_SETTLE_SECONDS = 20.0
# Refusal message prefixes from litellm/proxy/auth/auth_checks.py and
# litellm/proxy/spend_tracking/budget_reservation.py in LiteLLM 1.109.1. Budget scopes the
# platform does not configure, such as TeamMember= or User=, are left unclassified.
_REFUSAL_REASONS = (
    ("Budget has been exceeded! Key=", RUN_MODEL_BUDGET_EXCEEDED),
    ("ExceededBudget: Key ", RUN_MODEL_BUDGET_EXCEEDED),
    ("Budget has been exceeded! Team=", TENANT_MODEL_BUDGET_EXCEEDED),
    ("ExceededBudget: Team=", TENANT_MODEL_BUDGET_EXCEEDED),
)
_RUN_KEY_PREFIX = "sk-run-"
_RUN_KEY_CONTEXT = "litellm-run-key:"
_REDACTED = "<redacted>"

log = logging.getLogger("litellm_admin")


class LiteLLMAdminError(RuntimeError):
    """The LiteLLM admin API refused a request or could not be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RunModelAccessError(RuntimeError):
    """The run cannot be issued a model key, so no pod of the run may start."""


class ModelBudgetExceededError(Exception):
    """LiteLLM refused the run's model calls because a run or tenant budget is spent."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"LiteLLM refused the run's model calls with {reason}")
        self.reason = reason


@dataclass(frozen=True)
class TeamLimits:
    max_budget: float = 0.0
    budget_duration: str = ""
    rpm_limit: int = 0
    tpm_limit: int = 0

    def fields(self) -> dict[str, Any]:
        """Every limit field of the team, with None for a limit configured as zero."""
        budget = self.max_budget if self.max_budget > 0 else None
        return {
            "max_budget": budget,
            "budget_duration": (self.budget_duration or None) if budget is not None else None,
            "rpm_limit": self.rpm_limit or None,
            "tpm_limit": self.tpm_limit or None,
        }

    def body(self) -> dict[str, Any]:
        """The team fields to set on creation. A zero limit is omitted."""
        return {name: value for name, value in self.fields().items() if value is not None}

    def changes_from(self, stored: dict[str, Any]) -> dict[str, Any]:
        """The fields /team/update must send to bring a stored team to these limits.

        Only fields whose stored value differs are sent, because LiteLLM recomputes the
        team's budget reset time whenever budget_duration is in the request. A limit now
        configured as zero is sent as null, which LiteLLM applies and so removes the limit.
        """
        return {name: value for name, value in self.fields().items() if stored.get(name) != value}


class LiteLLMAdminClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        *,
        timeout: float = _TIMEOUT_SECONDS,
        attempts: int = _ATTEMPTS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._master_key = master_key
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._transport = transport
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Any) -> LiteLLMAdminClient:
        from scripts.config import to_host_url

        return cls(to_host_url(settings.litellm_base_url, settings), settings.litellm_master_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        missing_ok: bool = False,
        secrets: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        headers = {"Authorization": f"Bearer {self._master_key}"}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers=headers,
        ) as client:
            attempt = 0
            while True:
                attempt += 1
                retrying = attempt < self._attempts
                try:
                    response = await client.request(method, path, json=json, params=params)
                except httpx.TransportError as exc:
                    if retrying:
                        await self._sleep(2.0 ** (attempt - 1))
                        continue
                    raise LiteLLMAdminError(
                        f"LiteLLM at {self.base_url} could not be reached for {method} {path} "
                        f"after {attempt} attempts ({type(exc).__name__})"
                    ) from exc
                if response.status_code in _RETRY_STATUSES and retrying:
                    await self._sleep(_retry_delay(response, attempt))
                    continue
                if response.status_code == httpx.codes.NOT_FOUND and missing_ok:
                    return None
                if response.is_error:
                    detail = _redact(response.text[:500], (self._master_key, *secrets))
                    raise LiteLLMAdminError(
                        f"LiteLLM answered {response.status_code} to {method} {path} ({detail})",
                        response.status_code,
                    )
                payload = response.json() if response.content else {}
                return payload if isinstance(payload, dict) else {}

    async def key_info(self, key: str) -> dict[str, Any] | None:
        """Return the key's record, or None when no live key has this value.

        The key travels in the POST body because the proxy access log records query strings.
        LiteLLM moves a deleted key to a separate table, so it reads as absent here and a run
        that reuses its execution id is issued the key again.
        """
        data = await self._request("POST", "/v2/key/info", json={"keys": [key]}, secrets=(key,))
        records = (data or {}).get("info")
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            return None
        return records[0]

    async def generate_key(
        self,
        *,
        key: str,
        alias: str,
        team_id: str,
        max_budget: float,
        duration: str,
        metadata: Mapping[str, Any],
    ) -> None:
        await self._request(
            "POST",
            "/key/generate",
            json={
                "key": key,
                "key_alias": alias,
                "team_id": team_id,
                "max_budget": max_budget,
                "duration": duration,
                "metadata": dict(metadata),
            },
            secrets=(key,),
        )

    async def update_key(self, *, key: str, duration: str) -> None:
        await self._request(
            "POST", "/key/update", json={"key": key, "duration": duration}, secrets=(key,)
        )

    async def delete_key(self, key: str) -> bool:
        """Delete the key and return whether it existed."""
        data = await self._request(
            "POST", "/key/delete", json={"keys": [key]}, missing_ok=True, secrets=(key,)
        )
        return data is not None

    async def team_info(self, team_id: str) -> dict[str, Any] | None:
        data = await self._request(
            "GET", "/team/info", params={"team_id": team_id}, missing_ok=True
        )
        if data is None:
            return None
        info = data.get("team_info")
        return info if isinstance(info, dict) else {}

    async def new_team(self, team_id: str, limits: TeamLimits) -> None:
        await self._request(
            "POST",
            "/team/new",
            json={"team_id": team_id, "team_alias": team_id, **limits.body()},
        )

    async def update_team(self, team_id: str, changes: dict[str, Any]) -> None:
        await self._request("POST", "/team/update", json={"team_id": team_id, **changes})


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "")
    return float(retry_after) if retry_after.isdigit() else 2.0 ** (attempt - 1)


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def derive_run_model_key(master_key: str, execution_id: str) -> str:
    """Derive the run's virtual key from the master key and the execution id.

    The value is recomputed wherever it is needed rather than stored or passed between
    activities, so it never enters Temporal history, and any activity of the run can rebuild
    it, including the one that deletes it when the run ends.
    """
    message = f"{_RUN_KEY_CONTEXT}{execution_id}".encode()
    digest = hmac.new(master_key.encode(), message, hashlib.sha256).hexdigest()
    return f"{_RUN_KEY_PREFIX}{digest}"


def run_key_alias(execution_id: str) -> str:
    return f"run-{execution_id}"


def run_key_metadata(tenant: str, execution_id: str) -> dict[str, Any]:
    """The run key's metadata. LiteLLM merges the key's tags into every request the key makes."""
    return {"tenant": tenant, "execution_id": execution_id, "tags": [tenant_tag(tenant)]}


def tenant_team_limits(settings: Any) -> TeamLimits:
    return TeamLimits(
        max_budget=settings.tenant_model_budget_usd,
        budget_duration=settings.tenant_model_budget_duration,
        rpm_limit=settings.tenant_model_rpm_limit,
        tpm_limit=settings.tenant_model_tpm_limit,
    )


_team_ready_until: dict[tuple[str, str, TeamLimits], float] = {}


async def ensure_tenant_team(
    client: LiteLLMAdminClient,
    tenant: str,
    limits: TeamLimits,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Create the tenant's team, or bring its limits to the configured values.

    The outcome is cached per process for a few minutes, so a run's pods do not each repeat
    the team calls.
    """
    cache_key = (client.base_url, tenant, limits)
    if _team_ready_until.get(cache_key, 0.0) > clock():
        return
    stored = await client.team_info(tenant)
    if stored is None:
        try:
            await client.new_team(tenant, limits)
        except LiteLLMAdminError as exc:
            # Another worker created the team between the lookup and the create.
            stored = (
                await client.team_info(tenant) if exc.status == httpx.codes.BAD_REQUEST else None
            )
            if stored is None:
                raise
    if stored is not None and (changes := limits.changes_from(stored)):
        await client.update_team(tenant, changes)
    _team_ready_until[cache_key] = clock() + _TEAM_CACHE_SECONDS


def _require_master_key(settings: Any) -> str:
    master_key = settings.litellm_master_key
    if not master_key:
        raise RunModelAccessError(
            "LITELLM_MASTER_KEY is empty, so no per-run model key can be derived or issued"
        )
    return master_key


async def ensure_run_model_key(
    execution_id: str,
    *,
    settings: Any = None,
    client: LiteLLMAdminClient | None = None,
) -> str:
    """Issue or renew the run's virtual key and return it for a pod to present.

    A key that exists has its expiry pushed out again, so a run parked for longer than the
    access TTL regains model access when it resumes.
    """
    from scripts.config import get_settings

    resolved = settings or get_settings()
    if not execution_id:
        raise RunModelAccessError("a per-run model key needs the run's execution id")
    tenant = resolved.tenant.strip()
    if not tenant:
        raise RunModelAccessError(
            "TENANT is empty, so the run has no LiteLLM team to issue its model key under. "
            "Set TENANT on the activity worker. Pods are never given the master key."
        )
    key = derive_run_model_key(_require_master_key(resolved), execution_id)
    admin = client or LiteLLMAdminClient.from_settings(resolved)
    await ensure_tenant_team(admin, tenant, tenant_team_limits(resolved))
    duration = f"{resolved.run_model_access_ttl_seconds}s"
    if await admin.key_info(key) is not None:
        await admin.update_key(key=key, duration=duration)
        return key
    try:
        await admin.generate_key(
            key=key,
            alias=run_key_alias(execution_id),
            team_id=tenant,
            max_budget=resolved.run_model_budget_usd,
            duration=duration,
            metadata=run_key_metadata(tenant, execution_id),
        )
    except LiteLLMAdminError as exc:
        # A concurrent activity of the same run issued the key first.
        if exc.status != httpx.codes.BAD_REQUEST or await admin.key_info(key) is None:
            raise
        await admin.update_key(key=key, duration=duration)
    return key


async def revoke_run_model_key(
    execution_id: str,
    *,
    settings: Any = None,
    client: LiteLLMAdminClient | None = None,
) -> bool:
    """Delete the run's virtual key and return whether one existed."""
    from scripts.config import get_settings

    resolved = settings or get_settings()
    key = derive_run_model_key(_require_master_key(resolved), execution_id)
    admin = client or LiteLLMAdminClient.from_settings(resolved)
    return await admin.delete_key(key)


def _spent(info: Mapping[str, Any] | None) -> bool:
    if not info:
        return False
    max_budget = info.get("max_budget")
    if max_budget is None:
        return False
    return float(info.get("spend") or 0.0) >= float(max_budget)


async def _spent_budget(admin: LiteLLMAdminClient, key: str, tenant: str) -> str:
    if _spent(await admin.key_info(key)):
        return RUN_MODEL_BUDGET_EXCEEDED
    if tenant and _spent(await admin.team_info(tenant)):
        return TENANT_MODEL_BUDGET_EXCEEDED
    return ""


def classify_budget_refusal(failure_text: str) -> str:
    """Name the budget a LiteLLM refusal message cites, or return an empty string."""
    for prefix, reason in _REFUSAL_REASONS:
        if prefix in failure_text:
            return reason
    return ""


async def run_model_budget_stop(
    execution_id: str,
    *,
    failure_text: str = "",
    settings: Any = None,
    client: LiteLLMAdminClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Name the budget that refused the run's model calls, or return an empty string.

    Called after a sandbox conversation or a gate engine fails. A refusal message in the
    failure text decides it without reading LiteLLM. Otherwise the spend LiteLLM recorded
    against the run key and the tenant team decides it. While neither reads as spent, the
    spend is read again every few seconds until the proxy's batched spend write has had time
    to land. A lookup that fails reports no budget stop at once, so the failure keeps its
    original reason.
    """
    # The proxy refuses a call when the recorded spend plus the reserved worst-case cost of
    # the calls in flight passes the budget, so a refused run can still read as under budget.
    refused = classify_budget_refusal(failure_text)
    if refused:
        return refused
    from scripts.config import get_settings

    resolved = settings or get_settings()
    waited = 0.0
    try:
        key = derive_run_model_key(_require_master_key(resolved), execution_id)
        admin = client or LiteLLMAdminClient.from_settings(resolved)
        tenant = resolved.tenant.strip()
        while True:
            reason = await _spent_budget(admin, key, tenant)
            if reason or waited >= _SPEND_SETTLE_SECONDS:
                return reason
            await sleep(_SPEND_SETTLE_INTERVAL_SECONDS)
            waited += _SPEND_SETTLE_INTERVAL_SECONDS
    except (LiteLLMAdminError, RunModelAccessError, httpx.HTTPError, ValueError) as exc:
        log.warning("could not read the LiteLLM budget of run %s (%s)", execution_id, exc)
    return ""


@dataclass(frozen=True)
class RunModelAccess:
    """The per-run key resolution a gate engine pod uses, bound to the worker's settings."""

    settings: Any = None

    async def key_for(self, execution_id: str) -> str:
        return await ensure_run_model_key(execution_id, settings=self.settings)

    async def budget_stop(self, execution_id: str, failure_text: str = "") -> str:
        return await run_model_budget_stop(
            execution_id, failure_text=failure_text, settings=self.settings
        )
