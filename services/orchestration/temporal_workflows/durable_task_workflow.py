"""Durable task workflow: persist state, invoke Bedrock, record audit events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.bedrock import (
        ModelRequest,
        ModelResponse,
        invoke_model,
    )
    from services.orchestration.temporal_activities.counter import count_tick
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )
    from services.orchestration.temporal_activities.planning import (
        ContextRequest,
        ContextResult,
        gather_repo_context,
    )


@dataclass
class DurableTaskInput:
    task_id: str
    execution_id: str
    prompt: str = "Reply with the single word: ready"
    repo: str = ""
    commit: str = ""
    focus: str = ""
    work_seconds: int = 0


@workflow.defn
class DurableTaskWorkflow:
    def __init__(self) -> None:
        self._context_package: dict[str, Any] = {}
        self._completed_steps = 0
        self._total_steps = 0

    @workflow.query
    def context_package(self) -> dict[str, Any]:
        return self._context_package

    @workflow.query
    def progress(self) -> dict[str, int]:
        return {"completed": self._completed_steps, "total": self._total_steps}

    @workflow.run
    async def run(self, params: DurableTaskInput) -> str:
        retry = RetryPolicy(maximum_attempts=3)
        forever = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=0)
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

        prompt = params.prompt
        if params.repo:
            context: ContextResult = await workflow.execute_activity(
                gather_repo_context,
                ContextRequest(repo=params.repo, commit=params.commit, focus=params.focus),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
            )
            self._context_package = context.package
            if context.summary:
                prompt = f"{context.summary}\n\n{params.prompt}"

        self._total_steps = params.work_seconds
        for step in range(1, params.work_seconds + 1):
            await workflow.execute_activity(
                count_tick,
                step,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=forever,
            )
            self._completed_steps = step

        response: ModelResponse = await workflow.execute_activity(
            invoke_model,
            ModelRequest(logical_model="classification-summary", prompt=prompt),
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
