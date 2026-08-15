"""Resolve the target repositories for a Jira intake ticket against the allowlist."""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = re.compile(r"^\s*repos?\s*:\s*(.+)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class TargetResolution:
    repos: tuple[str, ...]
    routable: bool
    reason: str


def _marker_names(description: str) -> tuple[str, ...] | None:
    match = _MARKER.search(description or "")
    if match is None:
        return None
    names = [part.strip() for part in match.group(1).split(",")]
    return tuple(name for name in names if name)


def _scanned_names(description: str, allowlist: frozenset[str]) -> tuple[str, ...]:
    found: list[str] = []
    for repo in sorted(allowlist):
        if re.search(rf"(?<![\w-]){re.escape(repo)}(?![\w-])", description or ""):
            found.append(repo)
    return tuple(found)


def resolve_targets(
    description: str,
    *,
    default_repo: str,
    allowlist: frozenset[str],
) -> TargetResolution:
    """Resolve target repos by precedence: description marker, then scan, then default.

    Validation is all-or-nothing. A marker or scan that names any repository outside the
    allowlist holds the whole ticket, and a ticket that resolves to no repository holds too.
    """
    marker = _marker_names(description)
    if marker is not None:
        candidates = marker
        source = "marker"
    else:
        scanned = _scanned_names(description, allowlist)
        if scanned:
            candidates = scanned
            source = "description"
        elif default_repo:
            candidates = (default_repo,)
            source = "default_repo"
        else:
            return TargetResolution(
                repos=(),
                routable=False,
                reason="ticket names no repository and the project has no default_repo",
            )

    unlisted = [repo for repo in candidates if repo not in allowlist]
    if unlisted:
        return TargetResolution(
            repos=(),
            routable=False,
            reason=f"{source} names repositories outside the allowlist: {', '.join(unlisted)}",
        )

    ordered = tuple(dict.fromkeys(candidates))
    return TargetResolution(repos=ordered, routable=True, reason=source)
