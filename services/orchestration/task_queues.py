"""The Temporal task queues each tenant's runs use.

Every run of a tenant runs on agent-platform-<tenant>, the base queue TEMPORAL_TASK_QUEUE with the
tenant appended. The shared workflow worker polls the base queue and every tenant queue that
TENANTS lists, and each tenant's activity worker polls its own queue only, so a run's activities,
which default to its workflow's queue, reach only its tenant's worker. Review activities keep the
explicit review queue.
"""

from __future__ import annotations

from typing import Any


def tenant_names(settings: Any) -> list[str]:
    return [name.strip() for name in settings.tenants.split(",") if name.strip()]


def tenant_task_queue(settings: Any, tenant: str) -> str:
    return f"{settings.temporal_task_queue}-{tenant}"


def run_task_queue(settings: Any) -> str:
    """Return the queue this process's tenant runs on, or the base queue without a tenant."""
    if not settings.tenant:
        return settings.temporal_task_queue
    return tenant_task_queue(settings, settings.tenant)


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
