"""List recent Temporal workflow executions."""

from __future__ import annotations

import asyncio

from scripts.config import get_settings, to_host_hostport


async def _list() -> int:
    settings = get_settings()
    try:
        from services import temporal_client
    except ImportError:
        print("temporalio not installed; run `pip install -e .`")
        return 1

    client = await temporal_client.connect(to_host_hostport(settings.temporal_host), settings)
    print(
        f"[workflow-status] namespace={settings.temporal_namespace} "
        f"queue={settings.temporal_task_queue}"
    )
    count = 0
    async for wf in client.list_workflows():
        print(f"  {wf.id}\t{wf.status}\t{wf.workflow_type}")
        count += 1
        if count >= 25:
            break
    if count == 0:
        print("  (no workflows yet)")
    return 0


def main() -> int:
    return asyncio.run(_list())


if __name__ == "__main__":
    raise SystemExit(main())
