"""Resume parked workflows that a newer agent build can retry, or one run parked on the model path.

Run once against Temporal. Without a target, reads the suspended_flows registry, plans which
parked workflows the current build is newer than, and signals each to resume. With ``--issue``
or ``--workflow-id``, resumes the tenant's run parked because the model path was unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from services import temporal_client
from services.orchestration.reconciler import reconcile
from services.orchestration.suspension import RESUME_SIGNAL, ResumeOn
from services.orchestration.suspension_store import PostgresSuspensionStore
from services.orchestration.worker_versioning import move_to_current_version

from scripts.config import get_settings, to_host_hostport

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("resume-parked")


async def main() -> None:
    settings = get_settings()
    address = to_host_hostport(settings.temporal_host)
    client = await temporal_client.connect(address, settings)

    async def send_resume(workflow_id: str, trigger: ResumeOn) -> None:
        handle = client.get_workflow_handle(workflow_id)
        await move_to_current_version(client, settings, workflow_id)
        await handle.signal(RESUME_SIGNAL, trigger.value)
        log.info(
            "resumed %s on build %s (trigger=%s)",
            workflow_id,
            settings.agent_build_id,
            trigger.value,
        )

    plans = await reconcile(
        PostgresSuspensionStore(),
        send_resume,
        current_build_id=settings.agent_build_id,
    )
    log.info("reconcile resumed %d parked workflow(s)", len(plans))


async def _resume_model_path_park(*, issue_key: str, workflow_id: str) -> int:
    from services.orchestration.model_path_parks import run_resume_parked_runs

    from scripts.operator_identity import local_operator

    result = await run_resume_parked_runs(
        issue_key=issue_key, workflow_id=workflow_id, actor=local_operator()
    )
    print(f"[resume-parked] {result.message}")
    return 0 if result.resumed else 1


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--issue", default="", help="Resume this issue's run parked on the model path."
    )
    target.add_argument(
        "--workflow-id", default="", help="Resume this workflow's run parked on the model path."
    )
    args = parser.parse_args()
    if args.issue or args.workflow_id:
        return asyncio.run(
            _resume_model_path_park(issue_key=args.issue, workflow_id=args.workflow_id)
        )
    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
