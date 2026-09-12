"""Temporal workflow that drives a single Jira intake reconcile pass."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.intake import (
        IntakeReconcileReport,
        IntakeReconcileRequest,
        reconcile_intake_pass,
    )


@dataclass
class IntakeInput:
    pass


@dataclass
class IntakeOutput:
    started: int
    duplicates: int
    held: int
    deferred: int = 0
    blocked: int = 0


@workflow.defn
class IntakeWorkflow:
    @workflow.run
    async def run(self, params: IntakeInput) -> IntakeOutput:
        report: IntakeReconcileReport = await workflow.execute_activity(
            reconcile_intake_pass,
            IntakeReconcileRequest(),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return IntakeOutput(
            started=report.started,
            duplicates=report.duplicates,
            held=report.held,
            deferred=report.deferred,
            blocked=report.blocked,
        )
