"""Scrub the git credential earlier releases persisted into execution workspace checkouts."""

from __future__ import annotations

import argparse
from pathlib import Path

from services.orchestration.workspace_scrub import scrub_workspace_credentials

from scripts.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite every execution workspace remote URL that carries a credential."
    )
    parser.add_argument(
        "--root",
        default="",
        help="Workspace root to scrub (empty uses EXECUTION_WORKSPACE_ROOT).",
    )
    args = parser.parse_args()
    root = Path(args.root or get_settings().execution_workspace_root)
    receipt = scrub_workspace_credentials(root)
    print(
        f"[scrub-workspaces] root={root} scanned={receipt.scanned} "
        f"rewritten={len(receipt.rewritten)} failed={len(receipt.failed)}"
    )
    for workspace in receipt.rewritten:
        print(f"  REWRITTEN {workspace}")
    for workspace in receipt.failed:
        print(f"  FAILED {workspace}")
    return 1 if receipt.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
