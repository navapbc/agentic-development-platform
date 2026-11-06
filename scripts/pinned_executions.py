"""Report the running executions pinned to one build of the worker deployment.

Exits 0 when none are pinned and PINNED_EXIT_CODE when some are, printing their ids, so a deploy
that runs this as a one-shot task can tell whether the build's workers may stop.
"""

from __future__ import annotations

import argparse
import asyncio

from services import temporal_client
from services.orchestration.worker_versioning import pinned_executions

from scripts.config import get_settings, to_host_hostport

PINNED_EXIT_CODE = 3


async def _pinned(build_id: str) -> list[str]:
    settings = get_settings()
    client = await temporal_client.connect(to_host_hostport(settings.temporal_host), settings)
    return await pinned_executions(client, settings.worker_deployment_name, build_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_id")
    args = parser.parse_args(argv)
    pinned = asyncio.run(_pinned(args.build_id))
    if not pinned:
        print(f"[pinned-executions] no running execution is pinned to {args.build_id}")
        return 0
    print(f"[pinned-executions] {len(pinned)} running execution(s) pinned to {args.build_id}:")
    for workflow_id in pinned:
        print(f"  {workflow_id}")
    return PINNED_EXIT_CODE


if __name__ == "__main__":
    temporal_client.exit_without_finalization(main())
