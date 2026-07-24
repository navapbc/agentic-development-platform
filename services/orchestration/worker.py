"""Temporal worker entry point."""

from __future__ import annotations

import asyncio
import logging

from scripts.config import get_settings
from temporalio.client import Client
from temporalio.worker import Worker

from services.orchestration.temporal_activities import (
    invoke_model,
    persist_execution_state,
    record_audit_event,
)
from services.orchestration.temporal_workflows import FoundationWorkflow

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("workflow-worker")


async def main() -> None:
    settings = get_settings()
    log.info(
        "connecting to Temporal at %s (ns=%s)",
        settings.temporal_host,
        settings.temporal_namespace,
    )
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[FoundationWorkflow],
        activities=[invoke_model, persist_execution_state, record_audit_event],
    )
    log.info("worker started on task queue '%s'", settings.temporal_task_queue)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
