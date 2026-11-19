"""Temporal activity that alerts humans when the repair loop is exhausted."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from temporalio import activity

from services.operator_remedy import Remedy


@dataclass
class AlertHumanInterventionRequest:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    reason: str
    stage: str
    detail: str = ""
    remedy: Remedy = field(default_factory=Remedy)
    park_key: str = ""
    """The park this alert announces. Empty when the alert offers no decision to act on."""


@dataclass
class AlertHumanInterventionResult:
    channels: list[str] = field(default_factory=list)
    remedy: str = ""
    """The remedy as the alert rendered it, for the audit record."""


@activity.defn
async def alert_human_intervention(
    req: AlertHumanInterventionRequest,
) -> AlertHumanInterventionResult:
    from scripts.config import get_settings

    from services.orchestration.notifications import (
        HumanInterventionAlert,
        render_remedy,
        send_human_intervention_alert,
    )

    settings = get_settings()
    remedy = render_remedy(req.remedy, settings)
    info = activity.info()
    deadline = None
    if info.start_to_close_timeout is not None:
        elapsed = (datetime.now(UTC) - info.started_time).total_seconds()
        deadline = time.monotonic() + info.start_to_close_timeout.total_seconds() - elapsed
    channels = await asyncio.to_thread(
        send_human_intervention_alert,
        HumanInterventionAlert(
            task_id=req.task_id,
            execution_id=req.execution_id,
            workflow_id=req.workflow_id,
            repo=req.repo,
            issue_key=req.issue_key,
            reason=req.reason,
            stage=req.stage,
            detail=req.detail,
            remedy=remedy,
            structured_remedy=req.remedy,
            park_key=req.park_key,
        ),
        settings,
        issued_at=int(info.scheduled_time.timestamp()),
        deadline=deadline,
    )
    return AlertHumanInterventionResult(channels=channels, remedy=remedy)
