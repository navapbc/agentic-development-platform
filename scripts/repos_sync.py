"""Copy the local-backend repositories and local-mode gate engines into the cluster.

Nothing on the workstation is mounted into a pod. Each repository with backend local in
config/<env>/repositories.yaml is sent as a git bundle over `kubectl exec` and fetched into its
copy at $REPOSITORY_ROOT/<name> on the workspace volume. The bundle leaves out the agent/*
branches and every commit the copy already holds, and the fetch only fast-forwards, so a sync
adds commits and never replaces a branch the agent wrote. Each local-mode engine in
gate_engine.yaml is sent as its working tree, uncommitted edits included, to
$GATE_ENGINE_ROOT/<name>.
"""

from __future__ import annotations

import argparse
import io
import shlex
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from services import process

from scripts.config import Settings, get_settings

LOG = "[repos-sync]"
AGENT_BRANCHES = "agent/*"

Runner = Callable[..., process.CompletedProcess[Any]]


def pod_refs_script(name: str) -> str:
    dest = f'"$REPOSITORY_ROOT"/{shlex.quote(name)}'
    return f"if [ -d {dest}/.git ]; then git -C {dest} for-each-ref --format='%(objectname)'; fi"


def receive_bundle_script(name: str, default_branch: str) -> str:
    dest = f'"$REPOSITORY_ROOT"/{shlex.quote(name)}'
    branch = shlex.quote(f"refs/heads/{default_branch}")
    return "\n".join(
        [
            "set -e",
            f"[ -d {dest}/.git ] || git init -q {dest}",
            'bundle="$(mktemp)"',
            "trap 'rm -f \"$bundle\"' EXIT",
            'cat > "$bundle"',
            f'git -C {dest} fetch -q --update-head-ok "$bundle" '
            "'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'",
            f"git -C {dest} checkout -q -f --detach {branch}",
        ]
    )


def receive_tree_script(name: str) -> str:
    dest = f'"$GATE_ENGINE_ROOT"/{shlex.quote(name)}'
    return "\n".join(
        [
            "set -e",
            f"rm -rf {dest}.next",
            f"mkdir -p {dest}.next",
            f"tar -x -C {dest}.next",
            f"rm -rf {dest}",
            f"mv {dest}.next {dest}",
        ]
    )


def bundle_argv(host: Path, known: Sequence[str]) -> list[str]:
    """Bundle the host's branches and tags, minus agent branches and commits the copy holds."""
    return [
        "git",
        "-C",
        str(host),
        "bundle",
        "create",
        "-",
        f"--exclude={AGENT_BRANCHES}",
        "--branches",
        "--tags",
        *(f"^{sha}" for sha in known),
    ]


def held_on_host(host: Path, shas: Sequence[str], run: Runner = process.run) -> list[str]:
    held = []
    for sha in shas:
        probe = run(
            ["git", "-C", str(host), "cat-file", "-e", f"{sha}^{{commit}}"],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
        )
        if probe.returncode == 0:
            held.append(sha)
    return held


def working_tree(host: Path, run: Runner = process.run) -> bytes:
    """Tar the files git tracks or would track, plus .git, so the copy matches the checkout."""
    listing = run(
        ["git", "-C", str(host), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=True,
    ).stdout.decode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(host / ".git", arcname=".git")
        for name in filter(None, listing.split("\0")):
            if (host / name).is_file():
                archive.add(host / name, arcname=name)
    return buffer.getvalue()


def sync_repository(
    settings: Settings, name: str, host: Path, default_branch: str, run: Runner = process.run
) -> str:
    from scripts.k8s_cluster import exec_argv

    refs = run(
        exec_argv(settings, ["sh", "-c", pod_refs_script(name)], tty=False),
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    bundle = run(
        bundle_argv(host, held_on_host(host, refs, run)),
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
    )
    if bundle.returncode != 0:
        if b"empty bundle" in bundle.stderr:
            return "up to date"
        raise SystemExit(f"{LOG} git bundle of {host} failed: {bundle.stderr.decode().strip()}")
    run(
        exec_argv(settings, ["sh", "-c", receive_bundle_script(name, default_branch)], tty=False),
        input=bundle.stdout,
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )
    return f"sent {len(bundle.stdout)} bytes"


def sync_engine(settings: Settings, name: str, host: Path, run: Runner = process.run) -> str:
    from scripts.k8s_cluster import exec_argv

    tree = working_tree(host, run)
    run(
        exec_argv(settings, ["sh", "-c", receive_tree_script(name)], tty=False),
        input=tree,
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )
    return f"sent {len(tree)} bytes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="", help="Sync only this repository or engine.")
    args = parser.parse_args(argv)

    from services.integration.repositories import LOCAL, load_repository_registry
    from services.orchestration.gates.engine_source import (
        LOCAL as LOCAL_ENGINE,
    )
    from services.orchestration.gates.engine_source import (
        enabled_engines,
        load_gate_engine_configs,
    )

    from scripts.k8s_cluster import REPO_ROOT

    settings = get_settings()
    registry = load_repository_registry(settings)
    synced = 0
    for name in sorted(registry.names(LOCAL)):
        if args.repo and name != args.repo:
            continue
        host = registry.host_path(name, REPO_ROOT)
        if host is None or not (host / ".git").exists():
            print(f"{LOG} {name} has no git checkout at {host}", file=sys.stderr)
            return 1
        repository = registry.require(name)
        print(f"{LOG} {name}: {sync_repository(settings, name, host, repository.default_branch)}")
        synced += 1
    for engine in enabled_engines(load_gate_engine_configs(settings)):
        if engine.mode != LOCAL_ENGINE or (args.repo and engine.name != args.repo):
            continue
        host = Path(engine.local_path)
        if not (host / ".git").exists():
            print(f"{LOG} engine {engine.name} has no git checkout at {host}", file=sys.stderr)
            return 1
        print(f"{LOG} engine {engine.name}: {sync_engine(settings, engine.name, host)}")
        synced += 1
    print(f"{LOG} synced {synced} source(s) into {settings.k8s_namespace}/activity-worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
