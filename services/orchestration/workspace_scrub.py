"""One-time removal of the git credential earlier releases wrote into execution workspaces.

Releases before the per-invocation credential helper cloned and fetched with the token inside
the remote URL, which git writes verbatim into the checkout's `.git/config`. A workspace is
keyed by execution id and nothing deletes it, so most checkouts are never materialized again
and never reach the scrub that runs on reuse. The sandbox provisioner copies the whole
workspace, `.git` included, into the agent pod, so every such checkout is a credential at rest
inside the trust boundary the agent-generated code runs in.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from services import process

log = logging.getLogger(__name__)

_URL_KEY_SUFFIXES = (".url", ".pushurl")
_CREDENTIAL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class WorkspaceScrubReceipt:
    scanned: int
    rewritten: tuple[str, ...]
    failed: tuple[str, ...]


def strip_userinfo(url: str) -> str:
    """Return `url` without the `user:password@` part an earlier release embedded in it.

    Only an HTTP remote is rewritten. An SSH remote carries a username with no secret, and a
    path carries neither.
    """
    split = urlsplit(url)
    if split.scheme not in _CREDENTIAL_SCHEMES or "@" not in split.netloc:
        return url
    host = split.netloc.rpartition("@")[2]
    return urlunsplit((split.scheme, host, split.path, split.query, split.fragment))


def _git_config(git_dir: Path, *args: str) -> process.CompletedProcess[str]:
    return process.run(
        ["git", "config", "--file", str(git_dir / "config"), *args],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def _remote_urls(git_dir: Path) -> dict[str, str]:
    listing = _git_config(git_dir, "--list")
    if listing.returncode != 0:
        raise RuntimeError((listing.stderr or listing.stdout).strip())
    entries: dict[str, str] = {}
    for line in listing.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key.startswith("remote."):
            continue
        if key.endswith(_URL_KEY_SUFFIXES):
            entries[key] = value
    return entries


def _scrub_checkout(git_dir: Path) -> bool:
    rewritten = False
    for key, url in _remote_urls(git_dir).items():
        clean = strip_userinfo(url)
        if clean == url:
            continue
        result = _git_config(git_dir, "--replace-all", key, clean)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip())
        rewritten = True
    return rewritten


def _checkouts(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, directories, _ in os.walk(root):
        if ".git" in directories and (Path(current) / ".git").is_dir():
            found.append(Path(current) / ".git")
            directories.clear()
            continue
        directories[:] = [name for name in directories if not name.startswith(".")]
    return sorted(found)


def scrub_workspace_credentials(root: Path) -> WorkspaceScrubReceipt:
    """Rewrite every remote URL under `root` that carries an embedded credential."""
    if not root.is_dir():
        return WorkspaceScrubReceipt(scanned=0, rewritten=(), failed=())
    rewritten: list[str] = []
    failed: list[str] = []
    checkouts = _checkouts(root)
    for git_dir in checkouts:
        workspace = str(git_dir.parent)
        try:
            if _scrub_checkout(git_dir):
                rewritten.append(workspace)
        except (OSError, RuntimeError) as exc:
            log.error("workspace credential scrub failed for %s: %s", workspace, exc)
            failed.append(workspace)
    return WorkspaceScrubReceipt(
        scanned=len(checkouts), rewritten=tuple(rewritten), failed=tuple(failed)
    )
