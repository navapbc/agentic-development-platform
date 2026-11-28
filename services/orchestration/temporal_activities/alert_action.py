from __future__ import annotations

from temporalio import activity

from services.integration.slack.dispatcher import AlertOutcome, AlertRequest, Refusal


@activity.defn
async def apply_alert_action(request: AlertRequest) -> AlertOutcome:
    from scripts.config import get_settings

    from services.orchestration.alert_actions import build_tenant_alert_actions

    settings = get_settings()
    if request.tenant != settings.tenant.strip():
        return AlertOutcome(refusal=Refusal.UNVERIFIED)
    return await build_tenant_alert_actions(settings, activity.client()).act(request)
