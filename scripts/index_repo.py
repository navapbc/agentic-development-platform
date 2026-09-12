"""Pre-index a repository in the Repository Intelligence Service before intake.

Planning fetches a context package and fails closed if the repo is not indexed,
and indexing is lazy and slow on a large repo. This command warms the index
ahead of time by calling the service's ``/warm`` endpoint, so the first run's
context request is served from a ready index.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from scripts.config import get_settings, to_host_url


def warm(repo: str, commit: str = "", *, base_url: str) -> dict:
    with httpx.Client(base_url=base_url, timeout=1800.0) as client:
        resp = client.post("/warm", json={"repo": repo, "commit": commit})
        resp.raise_for_status()
        return resp.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-index a repo in repository intelligence.")
    parser.add_argument("--repo", required=True, help="Allowlisted repository name to index.")
    parser.add_argument("--commit", default="", help="Commit to index (empty uses the head).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    base_url = to_host_url(settings.repo_intelligence_url)
    result = warm(args.repo, args.commit, base_url=base_url)
    reindexed = result.get("reindexed")
    print(f"[index-repo] {args.repo}: reindexed={reindexed} at {base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
