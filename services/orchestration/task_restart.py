"""Abandon a parked workflow and re-trigger Jira intake in one step."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from scripts.config import get_settings, to_host_url
from sqlalchemy.ext.asyncio import create_async_engine

from services.orchestration.intake_cursor import (
    IntakeCursorRelease,
    build_intake_cursor_release,
)
from services.orchestration.intake_retry import (
    IntakeRetryResult,
    IntakeRetryStore,
    PostgresIntakeRetryStore,
    WorkflowLiveness,
    WorkflowLivenessProbe,
    retry_intake,
    temporal_workflow_liveness,
)
from services.orchestration.repair_decision import (
    PostgresRepairDecisionStore,
    RepairDecisionStore,
    RepairSignaller,
    TemporalRepairSignaller,
    resume_repair,
)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RestartResult:
    issue_key: str
    abandoned: tuple[str, ...]
    retry: IntakeRetryResult

    @property
    def restarted(self) -> bool:
        return self.retry.retriggered

    @property
    def message(self) -> str:
        if self.retry.blocked_by:
            running = ", ".join(self.retry.blocked_by)
            return f"{self.issue_key} still has a running workflow ({running}), not restarted"
        if self.retry.undetermined:
            unreachable = ", ".join(self.retry.undetermined)
            return (
                f"{self.issue_key} could not determine whether {unreachable} is still "
                "running, not restarted"
            )
        head = (
            f"abandoned {', '.join(self.abandoned)}"
            if self.abandoned
            else "no parked workflow to abandon"
        )
        return f"{self.issue_key} restarted, {head}. {self.retry.message}"


async def restart_task(
    issue_key: str,
    *,
    actor: str = "operator",
    repo: str | None = None,
    repair_store: RepairDecisionStore,
    repair_signaller: RepairSignaller,
    intake_store: IntakeRetryStore,
    liveness: WorkflowLivenessProbe,
    cursor: IntakeCursorRelease | None = None,
    settle_attempts: int = 40,
    settle_delay: float = 0.5,
    sleep: Sleep = asyncio.sleep,
) -> RestartResult:
    decision = await resume_repair(
        issue_key, retry=False, actor=actor, store=repair_store, signaller=repair_signaller
    )
    for workflow_id in decision.signalled:
        for _ in range(max(1, settle_attempts)):
            if await liveness(workflow_id) is WorkflowLiveness.ENDED:
                break
            await sleep(settle_delay)
    retry = await retry_intake(
        issue_key, store=intake_store, liveness=liveness, repo=repo, cursor=cursor
    )
    return RestartResult(issue_key, abandoned=decision.signalled, retry=retry)


async def run_restart_task(
    issue_key: str, *, actor: str = "operator", repo: str | None = None, settings: Any = None
) -> RestartResult:
    resolved = settings or get_settings()
    engine = create_async_engine(
        to_host_url(resolved.database_url), future=True, pool_pre_ping=True
    )
    try:
        return await restart_task(
            issue_key,
            actor=actor,
            repo=repo,
            repair_store=PostgresRepairDecisionStore(engine),
            repair_signaller=TemporalRepairSignaller(resolved),
            intake_store=PostgresIntakeRetryStore(engine),
            liveness=temporal_workflow_liveness(resolved),
            cursor=build_intake_cursor_release(resolved),
        )
    finally:
        await engine.dispose()
