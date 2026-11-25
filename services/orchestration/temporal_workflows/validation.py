from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.validation import validate_tenant
    from services.validation.report import TenantReport
    from services.validation.tenant import TenantValidationRequest


@workflow.defn
class TenantValidationWorkflow:
    """Started on the tenant's task queue, so its activity reaches only that tenant's worker.

    A worker that does not take the activity within a minute fails the run, which the platform
    validation reports as a tenant whose validation did not finish.
    """

    @workflow.run
    async def run(self, request: TenantValidationRequest) -> TenantReport:
        return await workflow.execute_activity(
            validate_tenant,
            request,
            schedule_to_start_timeout=timedelta(minutes=1),
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
