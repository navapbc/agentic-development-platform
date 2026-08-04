"""Resume parked workflows that a newer agent build can retry.

Run once against Temporal. Reads the suspended_flows registry, plans which parked
workflows the current build is newer than, and signals each to resume.
"""

from __future__ import annotations

import asyncio
import logging

from services.orchestration.reconciler import reconcile
from services.orchestration.suspension import RESUME_SIGNAL, ResumeOn
from services.orchestration.suspension_store import PostgresSuspensionStore
from temporalio.client import Client

from scripts.config import get_settings, to_host_hostport

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("resume-parked")


async def main() -> None:
    settings = get_settings()
    address = to_host_hostport(settings.temporal_host)
    client = await Client.connect(address, namespace=settings.temporal_namespace)

    async def send_resume(workflow_id: str, trigger: ResumeOn) -> None:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(RESUME_SIGNAL, trigger.value)
        log.info("resumed %s (trigger=%s)", workflow_id, trigger.value)

    plans = await reconcile(
        PostgresSuspensionStore(),
        send_resume,
        current_build_id=settings.agent_build_id,
    )
    log.info("reconcile resumed %d parked workflow(s)", len(plans))


if __name__ == "__main__":
    asyncio.run(main())
