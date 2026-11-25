from __future__ import annotations

from temporalio import activity

from services.validation.report import TenantReport
from services.validation.tenant import TenantValidationRequest


@activity.defn
async def validate_tenant(request: TenantValidationRequest) -> TenantReport:
    from scripts.config import get_settings

    from services.validation.tenant import validate_tenant as run

    return await run(get_settings(), request, client=activity.client())
