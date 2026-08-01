"""Durable counter workflow: count to a target, one activity per number."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.counter import count_tick


@dataclass
class CountingInput:
    target: int = 50


@workflow.defn
class CountingWorkflow:
    def __init__(self) -> None:
        self._count = 0

    @workflow.query
    def current_count(self) -> int:
        return self._count

    @workflow.run
    async def run(self, params: CountingInput) -> int:
        for n in range(1, params.target + 1):
            await workflow.execute_activity(
                count_tick,
                n,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    maximum_attempts=0,
                ),
            )
            self._count = n
        return self._count
