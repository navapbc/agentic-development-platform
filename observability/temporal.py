"""Temporal interceptor that stamps platform identifiers onto activity spans.

The OpenTelemetry ``TracingInterceptor`` creates one span per workflow and per
activity and connects them into a single trace. This interceptor runs inside the
activity span and copies the ``execution_id`` and ``task_id`` from the activity
input onto the current span and into baggage, so the identifiers reach the span
and every child span the activity creates.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import Any

from opentelemetry import trace
from temporalio.client import Interceptor as ClientInterceptor
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
)
from temporalio.worker import (
    Interceptor as WorkerInterceptor,
)

from observability.attributes import EXECUTION_ID, TASK_ID
from observability.context import bind_execution


def _extract_identifiers(args: Sequence[Any]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for arg in args:
        for key in (EXECUTION_ID, TASK_ID):
            if key in identifiers:
                continue
            value = arg.get(key) if isinstance(arg, dict) else getattr(arg, key, None)
            if value:
                identifiers[key] = str(value)
    return identifiers


class _PlatformActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        identifiers = _extract_identifiers(input.args)
        if identifiers:
            span = trace.get_current_span()
            for key, value in identifiers.items():
                with contextlib.suppress(Exception):
                    span.set_attribute(key, value)
            with bind_execution(**identifiers):
                return await super().execute_activity(input)
        return await super().execute_activity(input)


class PlatformTelemetryInterceptor(ClientInterceptor, WorkerInterceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _PlatformActivityInbound(next)
