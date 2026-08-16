"""Parse, authenticate, and route GitHub PR-closed webhooks to workflow signals."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import executions
from scripts.config import Settings, get_settings, to_host_hostport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from services.orchestration.webhook.inbound import DispatchNotReady

PR_CLOSED_SIGNAL = "submit_pr_closed"
CI_RESULT_SIGNAL = "submit_ci_result"
_SIGNATURE_PREFIX = "sha256="
_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_BRANCH_PREFIX = "agent/"


@dataclass(frozen=True)
class PrClosedEvent:
    repo: str
    number: int
    branch: str
    task_id: str
    merged: bool


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret:
        return True
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"{_SIGNATURE_PREFIX}{digest}", header)


def _task_id_from_branch(branch: str) -> str:
    return branch[len(_BRANCH_PREFIX) :] if branch.startswith(_BRANCH_PREFIX) else ""


def parse_pr_closed_event(payload: dict[str, Any]) -> PrClosedEvent | None:
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
        task_id=_task_id_from_branch(branch),
        merged=bool(pull.get("merged")),
    )


class WorkflowResolver(Protocol):
    async def resolve(self, event: PrClosedEvent) -> str | None: ...


class WorkflowSignaller(Protocol):
    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None: ...


class PrClosedHandler:
    name = "pr_closed"

    def __init__(
        self, *, secret: str, resolver: WorkflowResolver, signaller: WorkflowSignaller
    ) -> None:
        self._secret = secret
        self._resolver = resolver
        self._signaller = signaller

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_signature(self._secret, body, headers.get(_SIGNATURE_HEADER, ""))

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        return headers.get(_DELIVERY_HEADER, "")

    def parse(self, payload: dict[str, Any]) -> PrClosedEvent | None:
        return parse_pr_closed_event(payload)

    def allowlisted(self, event: PrClosedEvent, allowed: frozenset[str]) -> bool:
        return event.repo in allowed

    async def dispatch(self, event: PrClosedEvent) -> dict[str, Any]:
        workflow_id = await self._resolver.resolve(event)
        if not workflow_id:
            raise DispatchNotReady("workflow not found")
        await self._signaller.signal_pr_closed(workflow_id, merged=event.merged)
        return {"status": "signalled", "workflow_id": workflow_id, "merged": event.merged}


class PostgresWorkflowResolver:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def resolve(self, event: PrClosedEvent) -> str | None:
        return await self.resolve_task(event.task_id)

    async def resolve_task(self, task_id: str) -> str | None:
        if not task_id:
            return None
        stmt = (
            select(executions.c.workflow_id)
            .where(executions.c.task_id == task_id)
            .where(executions.c.workflow_id.is_not(None))
            .order_by(executions.c.updated_at.desc())
            .limit(1)
        )
        engine = self._get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return row.workflow_id if row is not None else None


class TemporalSignaller:
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    async def connect(cls, settings: Settings | None = None) -> TemporalSignaller:
        from temporalio.client import Client

        resolved = settings or get_settings()
        client = await Client.connect(
            to_host_hostport(resolved.temporal_host), namespace=resolved.temporal_namespace
        )
        return cls(client)

    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(PR_CLOSED_SIGNAL, merged)

    async def signal_ci_result(self, workflow_id: str) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(CI_RESULT_SIGNAL)
