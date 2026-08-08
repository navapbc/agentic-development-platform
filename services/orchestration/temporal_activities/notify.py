"""Temporal activity that alerts humans when the repair loop is exhausted."""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import activity


@dataclass
class AlertHumanInterventionRequest:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    attempts: int
    ci_build_id: str
    failure_summary: str = ""


@dataclass
class AlertHumanInterventionResult:
    channels: list[str] = field(default_factory=list)


@activity.defn
async def alert_human_intervention(
    req: AlertHumanInterventionRequest,
) -> AlertHumanInterventionResult:
    from services.orchestration.notifications import (
        HumanInterventionAlert,
        send_human_intervention_alert,
    )

    channels = send_human_intervention_alert(
        HumanInterventionAlert(
            task_id=req.task_id,
            execution_id=req.execution_id,
            workflow_id=req.workflow_id,
            repo=req.repo,
            issue_key=req.issue_key,
            attempts=req.attempts,
            ci_build_id=req.ci_build_id,
            failure_summary=req.failure_summary,
        )
    )
    return AlertHumanInterventionResult(channels=channels)
