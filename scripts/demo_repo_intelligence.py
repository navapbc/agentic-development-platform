"""Standalone repository-intelligence demo: build a briefing pack of a repo's most relevant code."""

from __future__ import annotations

import argparse
import asyncio

from services.orchestration.repo_intelligence_client import fetch_context_package
from services.repository_intelligence.normalization.serialization import render_context_summary

from scripts.config import get_settings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="agentic-demo-service", help="Repo to query.")
    parser.add_argument("--commit", default="", help="Commit to pin (empty uses the indexed head).")
    parser.add_argument(
        "--focus", default="", help="What to center the pack on, e.g. a function or file."
    )
    return parser.parse_args()


def _count(package: dict, key: str) -> int:
    value = package.get(key)
    return len(value) if isinstance(value, list) else 0


async def main() -> int:
    args = _parse_args()
    settings = get_settings()
    print(f"[ris] service={settings.repo_intelligence_url}")
    commit_label = args.commit or "indexed-head"
    print(f"[ris] repo={args.repo} commit={commit_label} focus={args.focus or '-'}")

    package = await fetch_context_package(args.repo, args.commit, args.focus)

    print("\n[ris] briefing pack (most relevant first, each item labeled by source):\n")
    print(render_context_summary(package))

    print("\n[ris] how many items of each kind it found:")
    labels = {
        "symbols": "key functions/classes",
        "candidate_files": "files likely to change",
        "callers": "what calls this code",
        "callees": "what this code calls",
        "related_tests": "related tests",
        "dependencies": "libraries used",
        "recent_changes": "recent changes",
    }
    for key, label in labels.items():
        print(f"  {label:<24} {_count(package, key)}")

    print("\nRIS DEMO: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
