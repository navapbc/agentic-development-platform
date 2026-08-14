"""Forward GitHub pull-request events to the local inbound-webhook through gh webhook forward.

gh webhook forward creates a dev webhook named "cli" on the target repository, dials the
GitHub-hosted relay, and posts each delivery to the local URL. Traffic stays inside GitHub,
so no third-party relay sits in the path, and GH_HOST is honored for GitHub Enterprise hosts.
Pass --remove to delete the dev hooks that gh webhook forward leaves on the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from services.integration.credentials import ConfigError, SecretNotFoundError
from services.integration.github.credentials import (
    UnconfiguredRepositoryError,
    build_github_credential_resolver,
)

from scripts.config import get_settings

EXTENSION = "cli/gh-webhook"


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, check=False, **kwargs)  # type: ignore[call-overload]


def _resolve_gh() -> str | None:
    gh = shutil.which("gh")
    if gh is None:
        print("[webhook] the GitHub CLI (gh) is not installed or not on PATH", file=sys.stderr)
    return gh


def _ensure_extension(gh: str) -> bool:
    listed = _run([gh, "extension", "list"], capture_output=True)
    if "gh-webhook" in (listed.stdout or ""):
        return True
    print(f"[webhook] installing the {EXTENSION} extension")
    return _run([gh, "extension", "install", EXTENSION]).returncode == 0


def _forward(gh: str, owner_repo: str, url: str, events: str, secret: str) -> int:
    cmd = [gh, "webhook", "forward", f"--events={events}", f"--repo={owner_repo}", f"--url={url}"]
    if secret:
        cmd.append(f"--secret={secret}")
    print(f"[webhook] forwarding {events} events from {owner_repo} to {url}")
    print("[webhook] leave this running during the demo, press Ctrl-C to stop")
    return _run(cmd).returncode


def _remove(gh: str, owner_repo: str) -> int:
    listed = _run([gh, "api", f"repos/{owner_repo}/hooks", "--paginate"], capture_output=True)
    if listed.returncode != 0:
        print(f"[webhook] could not list hooks for {owner_repo}", file=sys.stderr)
        print(listed.stderr or "", file=sys.stderr)
        return listed.returncode
    cli_hooks = [h for h in json.loads(listed.stdout or "[]") if h.get("name") == "cli"]
    if not cli_hooks:
        print(f"[webhook] no gh webhook forward hooks found on {owner_repo}")
        return 0
    for hook in cli_hooks:
        hook_id = hook["id"]
        deleted = _run([gh, "api", "-X", "DELETE", f"repos/{owner_repo}/hooks/{hook_id}"])
        if deleted.returncode != 0:
            print(f"[webhook] failed to remove dev hook {hook_id}", file=sys.stderr)
            return deleted.returncode
        print(f"[webhook] removed dev hook {hook_id}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward GitHub pull-request events locally")
    parser.add_argument("--repo", default=os.environ.get("DEMO_REPO", "agentic-demo-service"))
    parser.add_argument("--url", default="http://localhost:8140/webhook/pr")
    parser.add_argument("--events", default="pull_request")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Delete gh webhook forward dev hooks and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()
    try:
        cred = build_github_credential_resolver(settings).resolve(args.repo)
    except (ConfigError, SecretNotFoundError, UnconfiguredRepositoryError) as exc:
        print(f"[webhook] {exc}", file=sys.stderr)
        return 1
    if cred is None:
        print(
            f"[webhook] {args.repo} is not allowlisted or has no configured owner",
            file=sys.stderr,
        )
        return 1
    owner_repo = f"{cred.owner}/{args.repo}"

    gh = _resolve_gh()
    if gh is None:
        return 1
    if not _ensure_extension(gh):
        print(f"[webhook] failed to install the {EXTENSION} extension", file=sys.stderr)
        return 1

    if args.remove:
        return _remove(gh, owner_repo)
    return _forward(gh, owner_repo, args.url, args.events, settings.github_webhook_secret)


if __name__ == "__main__":
    raise SystemExit(main())
