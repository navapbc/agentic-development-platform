"""Resolve the target repositories for a Jira intake ticket against the allowlist."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

_MARKER = re.compile(r"^\s*repos?\s*:\s*(.+)\s*$", re.IGNORECASE | re.MULTILINE)


class ComponentMap(Protocol):
    def repos_for(self, component: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class TargetResolution:
    repos: tuple[str, ...]
    routable: bool
    reason: str
    unlisted: tuple[str, ...] = ()
    """Repositories the ticket named that the allowlist does not carry.

    Empty when nothing named a repository at all. The two cases need opposite advice, so the
    operator alert branches on this rather than on the reason text.
    """


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


def _component_names(components: Sequence[str], component_map: ComponentMap) -> tuple[str, ...]:
    found: list[str] = []
    for component in components:
        found.extend(component_map.repos_for(component))
    return tuple(dict.fromkeys(found))


def resolve_targets(
    description: str,
    *,
    default_repo: str,
    allowlist: frozenset[str],
    components: Sequence[str] = (),
    component_map: ComponentMap | None = None,
) -> TargetResolution:
    """Resolve target repos by precedence: marker, component mapping, scan, then default.

    An explicit ``repos:`` marker is the human override. With no marker, the ticket's Jira
    components map to repositories through the per-project component map. With neither, a
    whole-word description scan names the set, and with none of those the project's
    ``default_repo`` is the single target. Validation is all-or-nothing. A marker, component
    mapping, or scan that names any repository outside the allowlist holds the whole ticket,
    and a ticket that resolves to no repository holds too.
    """
    marker = _marker_names(description)
    if marker is not None:
        candidates = marker
        source = "marker"
    else:
        mapped = _component_names(components, component_map) if component_map is not None else ()
        if mapped:
            candidates = mapped
            source = "components"
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
            unlisted=tuple(unlisted),
        )

    ordered = tuple(dict.fromkeys(candidates))
    return TargetResolution(repos=ordered, routable=True, reason=source)
