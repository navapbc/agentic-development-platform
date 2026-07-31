"""Temporal workflow that drives a single reap pass over expired platform orphan artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.reap import (
        ReapReport,
        ReapRequest,
        reap_orphans,
    )


@dataclass
class ReapInput:
    dry_run: bool = False


@dataclass
class ReapOutput:
    dry_run: bool
    scanned: int
    applied: int


@workflow.defn
class ReapWorkflow:
    @workflow.run
    async def run(self, params: ReapInput) -> ReapOutput:
        report: ReapReport = await workflow.execute_activity(
            reap_orphans,
            ReapRequest(dry_run=params.dry_run),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        return ReapOutput(
            dry_run=report.dry_run,
            scanned=report.scanned,
            applied=report.applied,
        )
