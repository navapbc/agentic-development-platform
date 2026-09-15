"""Decide which security findings the change under review is answerable for.

The scanners read the whole checkout, so on an established repository every pre-existing finding
would set a scanner verdict and block promotion. Attribution keeps the scan repository-wide, and
narrows only the set of findings allowed to fail the gate, so the evidence record still carries
everything the tools reported.

Attribution is fail-safe. Whenever the change set or a finding's location cannot be established,
the finding keeps its power to block, and the reason is logged so a broken attribution is visible
rather than silently restoring repository-wide blocking.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable, Sequence
from typing import Protocol

logger = logging.getLogger(__name__)

CHANGED_SCOPE = "changed"
REPOSITORY_SCOPE = "repository"
SCAN_SCOPES = frozenset({CHANGED_SCOPE, REPOSITORY_SCOPE})


class GitRunner(Protocol):
    async def __call__(self, args: Sequence[str], cwd: str) -> tuple[int, str, str]: ...


async def _run_git(args: Sequence[str], cwd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd or None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def changed_paths(
    repo_root: str, base_ref: str, ref: str, *, runner: GitRunner | None = None
) -> frozenset[str] | None:
    """Repository-relative paths the change touched, or None when that cannot be established."""
    if not base_ref or not ref:
        logger.warning(
            "security scan scope falling back to the repository: "
            "base_ref=%r ref=%r does not describe a range",
            base_ref,
            ref,
        )
        return None
    run = runner or _run_git
    args = ["git", "diff", "--name-only", f"{base_ref}..{ref}"]
    try:
        code, out, err = await run(args, repo_root)
    except OSError as exc:
        logger.warning(
            "security scan scope falling back to the repository: %s failed to start: %s",
            " ".join(args),
            exc,
        )
        return None
    if code != 0:
        logger.warning(
            "security scan scope falling back to the repository: %s exited %d: %s",
            " ".join(args),
            code,
            err.strip(),
        )
        return None
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


def relative_location_path(location: str, repo_root: str) -> str:
    """The repository-relative file a finding's location names, or "" when it names none.

    Semgrep reports `path:line` and trivy reports `target:package@version`, so the path is the
    segment before the first colon. Pod mode rewrites its locations onto the worker's checkout
    path, which makes them absolute, so the checkout prefix is stripped when present.
    """
    head = location.split(":", 1)[0].strip()
    if not head:
        return ""
    root = repo_root.rstrip("/")
    if root and head.startswith(f"{root}/"):
        head = head[len(root) + 1 :]
    return head


def is_attributable(finding, changed: frozenset[str], repo_root: str) -> bool:
    """Whether a finding may set a scanner verdict, given the paths the change touched."""
    path = relative_location_path(finding.location or "", repo_root)
    if not path:
        logger.warning(
            "security finding %s carries no location, so it keeps its power to block",
            finding.finding_id,
        )
        return True
    if path in changed:
        return True
    if not os.path.isfile(os.path.join(repo_root, path)):
        logger.warning(
            "security finding %s names %r, which is not a file in the checkout, "
            "so it keeps its power to block",
            finding.finding_id,
            path,
        )
        return True
    return False


def blocking_findings(findings: Iterable, changed: frozenset[str] | None, repo_root: str) -> list:
    """The findings allowed to set a scanner verdict."""
    if changed is None:
        return list(findings)
    return [f for f in findings if is_attributable(f, changed, repo_root)]
