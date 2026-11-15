"""Parse, authenticate, and route GitHub PR-closed webhooks to workflow signals."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from database.engine import shared_platform_engine
from database.models.schema import executions
from scripts.config import Settings, get_settings, to_host_hostport
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from services.orchestration.webhook.inbound import DispatchNotReady

PR_CLOSED_SIGNAL = "submit_pr_closed"
_AWAITING_PR_CLOSE = "AWAITING_PR_CLOSE"
_TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED")
_SIGNATURE_PREFIX = "sha256="
_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_BRANCH_PREFIX = "agent/"
# Each request runs in its own asyncio task with its own context, so the tenant verify selects
# reaches parse of the same delivery and never another concurrent one.
_selected_tenant: ContextVar[str | None] = ContextVar("pr_closed_selected_tenant", default=None)


@dataclass(frozen=True)
class PrClosedEvent:
    repo: str
    number: int
    branch: str
    task_id: str
    merged: bool
    tenant: str | None = None


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret:
        return False
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"{_SIGNATURE_PREFIX}{digest}", header)


def _task_id_from_branch(branch: str, tenant: str | None = None) -> str:
    if not branch.startswith(_BRANCH_PREFIX):
        return ""
    rest = branch[len(_BRANCH_PREFIX) :]
    if tenant:
        tenant_prefix = f"{tenant}/"
        if rest.startswith(tenant_prefix):
            return rest[len(tenant_prefix) :]
    return rest


def parse_pr_closed_event(
    payload: dict[str, Any], tenant: str | None = None
) -> PrClosedEvent | None:
    if payload.get("action") != "closed":
        return None
    pull = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    number = pull.get("number")
    repo = repository.get("name")
    if not isinstance(number, int) or not repo:
        return None
    branch = str((pull.get("head") or {}).get("ref", ""))
    return PrClosedEvent(
        repo=str(repo),
        number=number,
        branch=branch,
        task_id=_task_id_from_branch(branch, tenant),
        merged=bool(pull.get("merged")),
        tenant=tenant,
    )


def repository_name(body: bytes) -> str | None:
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    repository = payload.get("repository")
    name = repository.get("name") if isinstance(repository, dict) else None
    return str(name) if name else None


class WorkflowResolver(Protocol):
    async def resolve(self, event: PrClosedEvent) -> str | None: ...


class WorkflowSignaller(Protocol):
    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None: ...


class RepositoryTenants(Protocol):
    def select_tenant(self, repository: str | None, body: bytes, signature: str) -> str | None: ...


@dataclass(frozen=True)
class PlatformRepositories:
    """Every repository belongs to the platform, and one secret signs every delivery."""

    secret: str

    def select_tenant(self, repository: str | None, body: bytes, signature: str) -> str | None:
        return "" if verify_signature(self.secret, body, signature) else None


class PrClosedHandler:
    """Route a PR-closed delivery to the tenant whose secret signs it.

    Several tenants may list the same repository, each with its own secret. The handler verifies
    the delivery against each claimant's secret and routes it to the one whose secret matches, so
    a tenant cannot signal the workflow of another tenant's repository. A delivery that no
    claimant's secret verifies is refused, and a delivery that more than one claimant's secret
    verifies is refused, because tenants that share a secret can forge each other's deliveries.
    """

    name = "pr_closed"

    def __init__(
        self,
        *,
        resolver: WorkflowResolver,
        signaller: WorkflowSignaller,
        secret: str = "",
        tenants: RepositoryTenants | None = None,
    ) -> None:
        self._tenants = tenants or PlatformRepositories(secret)
        self._resolver = resolver
        self._signaller = signaller

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        signature = headers.get(_SIGNATURE_HEADER, "")
        tenant = self._tenants.select_tenant(repository_name(body), body, signature)
        _selected_tenant.set(tenant)
        return tenant is not None

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        return headers.get(_DELIVERY_HEADER, "")

    def parse(self, payload: dict[str, Any]) -> PrClosedEvent | None:
        return parse_pr_closed_event(payload, tenant=_selected_tenant.get())

    def allowlisted(self, event: PrClosedEvent, allowed: frozenset[str]) -> bool:
        return event.tenant is not None and event.repo in allowed

    async def dispatch(self, event: PrClosedEvent) -> dict[str, Any]:
        workflow_id = await self._resolver.resolve(event)
        if not workflow_id:
            raise DispatchNotReady("workflow not found")
        await self._signaller.signal_pr_closed(workflow_id, merged=event.merged)
        return {"status": "signalled", "workflow_id": workflow_id, "merged": event.merged}


class PostgresWorkflowResolver:
    """Find the open workflow of the event's task among the executions of the event's tenant.

    An event without a tenant, the platform's, matches by task id alone. The executions recorded
    before executions carried a tenant have none, and they match only unattributed_tenant, the one
    tenant a single-tenant webhook serves.
    """

    def __init__(self, engine: AsyncEngine | None = None, *, unattributed_tenant: str = "") -> None:
        self._engine = engine
        self._unattributed_tenant = unattributed_tenant

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = shared_platform_engine()
        return self._engine

    async def resolve(self, event: PrClosedEvent) -> str | None:
        task_id = event.task_id
        if not task_id:
            return None
        stmt = (
            select(executions.c.workflow_id)
            .where(executions.c.task_id == task_id)
            .where(*self._tenant_scope(event.tenant))
            .where(executions.c.workflow_id.is_not(None))
            .where(executions.c.state.not_in(_TERMINAL_STATES))
            .order_by(
                (executions.c.state == _AWAITING_PR_CLOSE).desc(),
                executions.c.updated_at.desc(),
            )
            .limit(1)
        )
        engine = self._get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return row.workflow_id if row is not None else None

    def _tenant_scope(self, tenant: str | None) -> list[Any]:
        if not tenant:
            return []
        if tenant == self._unattributed_tenant:
            return [or_(executions.c.tenant == tenant, executions.c.tenant.is_(None))]
        return [executions.c.tenant == tenant]


class TemporalSignaller:
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    async def connect(cls, settings: Settings | None = None) -> TemporalSignaller:
        from services import temporal_client

        resolved = settings or get_settings()
        client = await temporal_client.connect(to_host_hostport(resolved.temporal_host), resolved)
        return cls(client)

    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None:
        from temporalio.service import RPCError, RPCStatusCode

        handle = self._client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(PR_CLOSED_SIGNAL, merged)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            raise
