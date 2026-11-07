"""Bring the agent branches and their published results back from the cluster.

The local backend commits to agent/* branches and refs/agent-results/* in its copy of each
repository on the workspace volume. This streams those refs as a git bundle over `kubectl exec`
and fetches them into the host checkout. An agent branch only fast-forwards, so a host edit to
one is reported instead of overwritten. The results refs are replaced, because the copy in the
cluster is their only writer.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services import process

from scripts.config import Settings, get_settings

LOG = "[fetch-results]"
FETCH_REFSPECS = (
    "refs/heads/agent/*:refs/heads/agent/*",
    "+refs/agent-results/*:refs/agent-results/*",
)

Runner = Callable[..., process.CompletedProcess[Any]]


def send_bundle_script(name: str) -> str:
    """Bundle the agent refs, leaving out the commits every other branch already carries."""
    dest = f'"$REPOSITORY_ROOT"/{shlex.quote(name)}'
    return "\n".join(
        [
            "set -e",
            f"[ -d {dest}/.git ] || exit 0",
            f'[ -n "$(git -C {dest} for-each-ref refs/heads/agent refs/agent-results)" ] || exit 0',
            f"git -C {dest} bundle create - --branches='agent/*' --glob='refs/agent-results/*' "
            "--not --exclude='agent/*' --branches --tags",
        ]
    )


def fetch_repository(settings: Settings, name: str, host: Path, run: Runner = process.run) -> str:
    from scripts.k8s_cluster import exec_argv

    bundle = run(
        exec_argv(settings, ["sh", "-c", send_bundle_script(name)], tty=False),
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=True,
    ).stdout
    if not bundle:
        return "no agent branches"
    with tempfile.NamedTemporaryFile(suffix=".bundle") as handle:
        handle.write(bundle)
        handle.flush()
        fetched = run(
            ["git", "-C", str(host), "fetch", handle.name, *FETCH_REFSPECS],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    if fetched.returncode != 0:
        raise SystemExit(f"{LOG} {name}: git fetch failed: {fetched.stderr.strip()}")
    listing = run(
        ["git", "-C", str(host), "for-each-ref", "--format=%(refname:short)", "refs/heads/agent"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return f"{len(listing)} agent branch(es): {', '.join(listing)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="", help="Fetch only this repository.")
    args = parser.parse_args(argv)

    from services.integration.repositories import LOCAL, load_repository_registry

    from scripts.k8s_cluster import REPO_ROOT

    settings = get_settings()
    registry = load_repository_registry(settings)
    names = [args.repo] if args.repo else sorted(registry.names(LOCAL))
    for name in names:
        if registry.require(name).backend != LOCAL:
            print(f"{LOG} {name} does not use the local backend", file=sys.stderr)
            return 1
        host = registry.host_path(name, REPO_ROOT)
        if host is None or not (host / ".git").exists():
            print(f"{LOG} {name} has no git checkout at {host}", file=sys.stderr)
            return 1
        print(f"{LOG} {name}: {fetch_repository(settings, name, host)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
