"""Remove the untenanted intake and reap schedules, which each tenant's worker now owns.

Each tenant's activity worker installs <source>-intake-<tenant> and <reap id>-<tenant> on its own
task queue when it starts. The deployment's schedule-bootstrap task runs this to delete the
schedules a deployment without tenants installed on the base queue.
"""

from __future__ import annotations

import asyncio

from scripts.config import get_settings


async def retire_untenanted(client=None) -> list[str]:
    from services import temporal_client
    from services.integration.tickets import TICKET_BACKENDS
    from temporalio.service import RPCError, RPCStatusCode

    settings = get_settings()
    if client is None:
        client = await temporal_client.connect(settings.temporal_host, settings)
    removed = []
    for schedule_id in [
        *(f"{source}-intake" for source in TICKET_BACKENDS),
        settings.reap_schedule_id,
    ]:
        try:
            await client.get_schedule_handle(schedule_id).delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            continue
        removed.append(schedule_id)
        print(f"[schedules] removed the untenanted schedule '{schedule_id}'")
    return removed


def main() -> int:
    asyncio.run(retire_untenanted())
    return 0


if __name__ == "__main__":
    from services.temporal_client import exit_without_finalization

    exit_without_finalization(main())
