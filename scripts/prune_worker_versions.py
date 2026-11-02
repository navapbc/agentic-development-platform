"""Delete the drained versions of the worker deployment.

Run against Temporal when a deployment has accumulated versions that no execution is pinned
to. A version the server keeps counts against the per-deployment version ceiling.
"""

from __future__ import annotations

import asyncio
import logging

from services.orchestration.worker_versioning import prune_drained_versions
from temporalio.client import Client

from scripts.config import get_settings, to_host_hostport

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("prune-worker-versions")


async def main() -> None:
    settings = get_settings()
    client = await Client.connect(
        to_host_hostport(settings.temporal_host), namespace=settings.temporal_namespace
    )
    deleted, kept = await prune_drained_versions(client, settings)
    log.info("deleted %d drained version(s): %s", len(deleted), ", ".join(deleted) or "none")
    if kept:
        log.warning(
            "%d drained version(s) could not be deleted and remain: %s",
            len(kept),
            ", ".join(kept),
        )


if __name__ == "__main__":
    asyncio.run(main())
