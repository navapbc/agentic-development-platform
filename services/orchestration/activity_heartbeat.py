"""Heartbeat every activity, so Temporal retries the activities of a worker that stopped.

An activity with no heartbeat timeout whose worker exits, as every worker does on a redeploy,
stays pending until its start-to-close timeout, which for the implementation stage is hours.
The workflow side gives every activity scheduled without a heartbeat timeout the default one,
and the activity side heartbeats from the worker's event loop while the activity runs. A worker
that exits stops heartbeating, and Temporal times the attempt out and schedules the next.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

ACTIVITY_HEARTBEAT_INTERVAL = timedelta(seconds=30)
# Six missed beats. An activity that blocks the event loop this long also times out, and its
# next attempt runs alongside it.
ACTIVITY_HEARTBEAT_TIMEOUT = timedelta(minutes=3)


class ActivityHeartbeatInterceptor(Interceptor):
    def __init__(self, interval: timedelta = ACTIVITY_HEARTBEAT_INTERVAL) -> None:
        self.interval = interval

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _HeartbeatingActivity(next, self.interval.total_seconds())

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _DefaultHeartbeatTimeoutWorkflow


class _HeartbeatingActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, interval_seconds: float) -> None:
        super().__init__(next)
        self._interval_seconds = interval_seconds

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        beat = asyncio.create_task(_beat(self._interval_seconds))
        try:
            return await super().execute_activity(input)
        finally:
            beat.cancel()


async def _beat(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        activity.heartbeat()


class _DefaultHeartbeatTimeoutWorkflow(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_DefaultHeartbeatTimeoutOutbound(outbound))


class _DefaultHeartbeatTimeoutOutbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle[Any]:
        if input.heartbeat_timeout is None:
            input = dataclasses.replace(input, heartbeat_timeout=ACTIVITY_HEARTBEAT_TIMEOUT)
        return super().start_activity(input)
