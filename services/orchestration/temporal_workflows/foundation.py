"""Foundation workflow: persist state, invoke Bedrock, record audit events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.bedrock import (
        ModelRequest,
        ModelResponse,
        invoke_model,
    )
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )


@dataclass
class FoundationInput:
    task_id: str
    execution_id: str
    prompt: str = "Reply with the single word: ready"
    pause_seconds: int = 0


@workflow.defn
class FoundationWorkflow:
    @workflow.run
    async def run(self, params: FoundationInput) -> str:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=3)

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state="TASK_RECEIVED",
                workflow_id=workflow.info().workflow_id,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        await workflow.execute_activity(
            record_audit_event,
            AuditEvent(
                event_id=f"{params.execution_id}:WORKFLOW_STARTED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="WORKFLOW_STARTED",
                new_state="TASK_RECEIVED",
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        if params.pause_seconds > 0:
            await workflow.sleep(timedelta(seconds=params.pause_seconds))

        response: ModelResponse = await workflow.execute_activity(
            invoke_model,
            ModelRequest(logical_model="classification-summary", prompt=params.prompt),
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state="COMPLETED",
                workflow_id=workflow.info().workflow_id,
                resolved_models={"classification-summary": response.model_id},
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        await workflow.execute_activity(
            record_audit_event,
            AuditEvent(
                event_id=f"{params.execution_id}:EXECUTION_COMPLETED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="EXECUTION_COMPLETED",
                prev_state="TASK_RECEIVED",
                new_state="COMPLETED",
                model_id=response.model_id,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        return response.content
