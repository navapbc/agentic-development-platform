"""Parse, authenticate, and route GitHub PR-closed webhooks to workflow signals."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import executions
from scripts.config import Settings, get_settings, to_host_hostport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

PR_CLOSED_SIGNAL = "submit_pr_closed"
_SIGNATURE_PREFIX = "sha256="
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


async def handle_pr_webhook(
    body: bytes,
    signature_header: str,
    *,
    secret: str,
    allowed_repos: frozenset[str],
    resolver: WorkflowResolver,
    signaller: WorkflowSignaller,
) -> tuple[int, dict[str, Any]]:
    if not verify_signature(secret, body, signature_header):
        return 401, {"error": "invalid signature"}
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return 400, {"error": "invalid json"}
    if not isinstance(payload, dict):
        return 400, {"error": "invalid payload"}
    event = parse_pr_closed_event(payload)
    if event is None:
        return 202, {"status": "ignored"}
    if event.repo not in allowed_repos:
        return 403, {"error": "repo not allowlisted"}
    workflow_id = await resolver.resolve(event)
    if not workflow_id:
        return 404, {"error": "workflow not found"}
    await signaller.signal_pr_closed(workflow_id, merged=event.merged)
    return 200, {"status": "signalled", "workflow_id": workflow_id, "merged": event.merged}


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
        if not event.task_id:
            return None
        stmt = (
            select(executions.c.workflow_id)
            .where(executions.c.task_id == event.task_id)
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
