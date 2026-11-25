from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.integration.slack.dispatcher import AlertOutcome, AlertRequest
    from services.orchestration.temporal_activities.alert_action import apply_alert_action


@workflow.defn
class AlertActionWorkflow:
    """Started on the tenant's task queue by the inbound webhook for a click it admitted.

    The activity runs once, because Restart run is guarded by the side-effect ledger and a
    repeated Acknowledge or Fix done is answered by the next click.
    """

    @workflow.run
    async def run(self, request: AlertRequest) -> AlertOutcome:
        return await workflow.execute_activity(
            apply_alert_action,
            request,
            schedule_to_start_timeout=timedelta(minutes=1),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
