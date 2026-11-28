"""The Temporal task queues each tenant's runs use.

Every run of a tenant runs on agent-platform-<tenant>, the base queue TEMPORAL_TASK_QUEUE with the
tenant appended. The shared workflow worker polls the base queue and every tenant queue that
TENANTS lists, and each tenant's activity worker polls its own queue only, so a run's activities,
which default to its workflow's queue, reach only its tenant's worker. A run's review-stage
activities run on agent-platform-review-<tenant>, the base REVIEW_TASK_QUEUE with the tenant
appended, which only that tenant's review worker polls.
"""

from __future__ import annotations

import re
from typing import Any

TENANT_NAME = re.compile(r"^(?!default$)[a-z][a-z0-9]{1,15}$")


def tenant_names(settings: Any) -> list[str]:
    return [name.strip() for name in settings.tenants.split(",") if name.strip()]


def tenant_task_queue(settings: Any, tenant: str) -> str:
    return f"{settings.temporal_task_queue}-{tenant}"


def run_task_queue(settings: Any) -> str:
    """Return the queue this process's tenant runs on, or the base queue without a tenant."""
    if not settings.tenant:
        return settings.temporal_task_queue
    return tenant_task_queue(settings, settings.tenant)


def review_task_queue(settings: Any, tenant: str) -> str:
    return f"{settings.review_task_queue}-{tenant}"


def run_review_task_queue(settings: Any) -> str:
    """Return the review queue of this process's tenant, or the base review queue without one."""
    if not settings.tenant:
        return settings.review_task_queue
    return review_task_queue(settings, settings.tenant)


def workflow_task_queues(settings: Any) -> list[str]:
    """Return the base queue and every tenant queue, which the workflow worker polls."""
    queues = [settings.temporal_task_queue]
    queues += [tenant_task_queue(settings, tenant) for tenant in tenant_names(settings)]
    if settings.tenant and run_task_queue(settings) not in queues:
        queues.append(run_task_queue(settings))
    return queues


def tenant_schedule_id(base: str, settings: Any) -> str:
    """Return a schedule's id for this process's tenant, which owns the schedule."""
    return f"{base}-{settings.tenant}" if settings.tenant else base
