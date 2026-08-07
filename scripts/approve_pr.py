"""Operator approval command: release the AWAITING_PR_APPROVAL pause on a running workflow.

Sends the human approval signal to a draft-PR workflow parked for review. With
--pr-closed it instead sends the PR-close signal, standing in for the GitHub webhook
when demoing the AWAITING_PR_CLOSE stage without a public ingress.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from temporalio.client import Client

from scripts.config import get_settings, to_host_hostport

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("approve-pr")

_APPROVAL_SIGNAL = "submit_pr_approval"
_PR_CLOSED_SIGNAL = "submit_pr_closed"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-id", required=True, help="Workflow id parked for approval.")
    parser.add_argument("--approver", default="release-manager", help="Approval identity.")
    parser.add_argument(
        "--reject",
        action="store_true",
        help="Reject the pull request instead of approving it.",
    )
    parser.add_argument(
        "--pr-closed",
        action="store_true",
        help="Send the PR-close signal for the AWAITING_PR_CLOSE stage.",
    )
    parser.add_argument(
        "--merged",
        action="store_true",
        help="With --pr-closed, mark the pull request as merged rather than closed unmerged.",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    settings = get_settings()
    address = to_host_hostport(settings.temporal_host)
    client = await Client.connect(address, namespace=settings.temporal_namespace)
    handle = client.get_workflow_handle(args.workflow_id)

    if args.pr_closed:
        await handle.signal(_PR_CLOSED_SIGNAL, args.merged)
        log.info("signalled %s pr-closed (merged=%s)", args.workflow_id, args.merged)
        return 0

    approved = not args.reject
    await handle.signal(_APPROVAL_SIGNAL, args=[approved, args.approver])
    log.info(
        "signalled %s approval (approved=%s approver=%s)",
        args.workflow_id,
        approved,
        args.approver,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
