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

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from services import process

logger = logging.getLogger(__name__)

CHANGED_SCOPE = "changed"
REPOSITORY_SCOPE = "repository"
SCAN_SCOPES = frozenset({CHANGED_SCOPE, REPOSITORY_SCOPE})


class GitRunner(Protocol):
    async def __call__(self, args: Sequence[str], cwd: str) -> tuple[int, str, str]: ...


async def _run_git(args: Sequence[str], cwd: str) -> tuple[int, str, str]:
    result = await process.run_async(
        args, timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS, cwd=cwd or None
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


async def _git_stdout(args: Sequence[str], repo_root: str, run: GitRunner) -> str | None:
    """Standard output of a git call, or None when it could not be run or failed."""
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
    return out


async def _git_paths(args: Sequence[str], repo_root: str, run: GitRunner) -> frozenset[str] | None:
    out = await _git_stdout(args, repo_root, run)
    if out is None:
        return None
    return frozenset(entry for entry in out.split("\0") if entry)


async def _merge_base(repo_root: str, base_ref: str, run: GitRunner) -> str | None:
    out = await _git_stdout(["git", "merge-base", base_ref, "HEAD"], repo_root, run)
    return None if out is None else out.strip() or base_ref


async def changed_paths(
    repo_root: str, base_ref: str, *, runner: GitRunner | None = None
) -> frozenset[str] | None:
    """Repository-relative paths the change touched, or None when that cannot be established.

    The comparison is against the working tree rather than a commit, because the scanners read
    the checkout at `repo_root`, and the change reaches the scan as uncommitted edits there when
    the gate runs ahead of publication.

    The left side is the merge base with `base_ref`, so a file that moved on the base branch after
    the change branched off does not carry its pre-existing findings into this change.

    Untracked files are unioned in. The scanners read them, so leaving them out would let a file
    the change added escape the gate.

    `-z` is what makes the paths comparable to a finding's location. Without it `git` applies
    `core.quotePath` and C-quotes any path outside ASCII, which would never match the location
    the scanner reported for the same file.
    """
    if not base_ref:
        logger.warning(
            "security scan scope falling back to the repository: "
            "base_ref=%r does not describe a range",
            base_ref,
        )
        return None
    run = runner or _run_git

    merge_base = await _merge_base(repo_root, base_ref, run)
    if merge_base is None:
        return None

    tracked = await _git_paths(["git", "diff", "--name-only", "-z", merge_base], repo_root, run)
    if tracked is None:
        return None
    untracked = await _git_paths(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root, run
    )
    if untracked is None:
        return None

    paths = tracked | untracked
    if not paths:
        logger.warning(
            "security scan scope falling back to the repository: the diff against %s reported "
            "no paths, so the range cannot be told apart from a mis-scoped one",
            merge_base,
        )
        return None
    return paths


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
    logger.info(
        "security finding %s sits in %r, which this change did not touch, "
        "so it is recorded as evidence without failing the gate",
        finding.finding_id,
        path,
    )
    return False


Packages = frozenset[tuple[str, str]]
_GO_REQUIREMENT = re.compile(r"^\s*(?:require\s+)?(\S+)\s+(v[^\s/]+)(?:/go\.mod)?(?:\s|$)")
_PINNED_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)"
)


def _python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _npm_packages(text: str) -> Packages:
    document = json.loads(text)
    found: set[tuple[str, str]] = set()
    for key, entry in (document.get("packages") or {}).items():
        if "node_modules/" in key and isinstance(entry, dict) and entry.get("version"):
            found.add((key.rsplit("node_modules/", 1)[1], str(entry["version"])))

    def walk(dependencies: Mapping) -> None:
        for name, entry in dependencies.items():
            if isinstance(entry, dict):
                if entry.get("version"):
                    found.add((name, str(entry["version"])))
                walk(entry.get("dependencies") or {})

    walk(document.get("dependencies") or {})
    return frozenset(found)


_GO_FILES = ("go.mod", "go.sum")


def _go_version(version: str) -> str:
    """A Go module version without its leading v, which go.mod and Trivy keep and OSV drops."""
    return version.removeprefix("v")


def packages_in(path: str, text: str) -> Packages | None:
    """The package and version pairs a dependency file names, or None for a format not read.

    A file whose format is not read leaves its findings the power to block.
    """
    name = os.path.basename(path)
    try:
        if name in ("package-lock.json", "npm-shrinkwrap.json"):
            return _npm_packages(text)
    except ValueError, AttributeError:
        return None
    if name in _GO_FILES:
        return frozenset(
            (m[1], _go_version(m[2]))
            for line in text.splitlines()
            if (m := _GO_REQUIREMENT.match(line))
        )
    if name.startswith("requirements") and name.endswith(".txt"):
        return frozenset(
            (_python_name(m[1]), m[2])
            for line in text.splitlines()
            if (m := _PINNED_REQUIREMENT.match(line))
        )
    return None


async def dependency_baseline(
    repo_root: str, base_ref: str, paths: Iterable[str], *, runner: GitRunner | None = None
) -> dict[str, Packages]:
    """The packages each dependency file named at the merge base with `base_ref`.

    A file the merge base lacks, or whose format is not read, has no entry, so every finding on it
    stays attributable to the change.
    """
    paths = sorted(set(paths))
    if not paths or not base_ref:
        return {}
    run = runner or _run_git
    merge_base = await _merge_base(repo_root, base_ref, run)
    if merge_base is None:
        return {}
    baseline: dict[str, Packages] = {}
    for path in paths:
        text = await _git_stdout(["git", "show", f"{merge_base}:{path}"], repo_root, run)
        packages = packages_in(path, text) if text is not None else None
        if packages is not None:
            baseline[path] = packages
    return baseline


def _in_baseline(finding, repo_root: str, baseline: Mapping[str, Packages]) -> bool:
    path = relative_location_path(finding.location or "", repo_root)
    spec = (finding.location or "").partition(":")[2]
    name, _, version = spec.rpartition("@")
    if path not in baseline or not name:
        return False
    if os.path.basename(path) in _GO_FILES:
        version = _go_version(version)
    names = {name, _python_name(name)}
    if any((candidate, version) in baseline[path] for candidate in names):
        logger.info(
            "dependency finding %s names %s@%s, which %s already held at the merge base, "
            "so it is recorded as evidence without failing the gate",
            finding.finding_id,
            name,
            version,
            path,
        )
        return True
    return False


def blocking_findings(
    findings: Iterable,
    changed: frozenset[str] | None,
    repo_root: str,
    *,
    baseline: Mapping[str, Packages] | None = None,
) -> list:
    """The findings allowed to set a scanner verdict.

    A dependency finding on a changed file blocks only when the merge-base copy of that file in
    `baseline` did not already name its package at that version.
    """
    from adapters.interfaces.security_scanner import DEPENDENCY

    if changed is None:
        return list(findings)
    return [
        f
        for f in findings
        if is_attributable(f, changed, repo_root)
        and not (baseline and f.kind == DEPENDENCY and _in_baseline(f, repo_root, baseline))
    ]
