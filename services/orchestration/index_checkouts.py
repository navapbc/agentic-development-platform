"""Keep a checkout of each indexed GitHub repository where repository intelligence reads it.

Repository intelligence indexes a tenant's repositories from ``<REPOSITORY_ROOT>/<name>``
through a read-only mount and holds no source-control credential, so the tenant's activity
worker keeps those checkouts. Each repository of the active registry with ``index: true`` and
the GitHub backend is checked out as a detached HEAD, at its ``index_ref`` when one is set and
otherwise at the tip of its default branch. ``inv repos-sync`` owns the checkouts of
local-backend repositories, and this module never writes them. A checkout whose repository
left the registry, or is no longer indexed, is logged and left in place.

The credential reaches git only through the per-invocation helper of ``GitAuth``, and the
remote URL carries no token. Every fetch runs with ``--no-write-fetch-head``, so neither
``.git/config`` nor ``FETCH_HEAD`` ever holds the credential. A default branch or a SHA pin is
fetched into a private ref. A tag or branch pin is fetched into the local ref of the same name,
which is moved on every pass, so the pin resolves inside the checkout to the commit HEAD names.
A pin that starts with ``-`` or is not a valid ref name is refused.

A first checkout is cloned into a hidden sibling directory and renamed into place, so the
index worker never sees a partial clone. An existing checkout is fetched and moved with
``git checkout --detach`` in place, which rewrites the working tree file by file and moves
HEAD only after the tree is written. For an unpinned repository the index worker indexes the
commit HEAD names, so a read that overlaps an update is superseded on its next poll, when HEAD
differs from the commit it indexed. A pinned checkout moves only when its ``index_ref``
changes. A copy per commit behind an atomic symlink swap would remove the overlap at the cost
of a full working tree per update, and was not chosen for that reason. Workers of one tenant
serialize on an advisory lock per repository, and a worker that finds the lock held skips the
repository until its next pass.

Files are written with the worker's umask under its uid, 10001, which the repository
intelligence containers share, so they read every checkout this module writes.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from scripts.config import Settings

from services import process
from services.integration.github.git_auth import NO_AUTH, GitAuth
from services.integration.repositories import GITHUB, LOCAL, Repository, RepositoryRegistry

log = logging.getLogger(__name__)

TARGET_REF = "refs/index-checkout/target"
_FULL_OR_ABBREVIATED_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class CheckoutLockedError(RuntimeError):
    """Raised when another worker holds the sync lock of a repository."""


@dataclass(frozen=True)
class SyncReport:
    synced: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    locked: tuple[str, ...] = ()
    unlisted: tuple[str, ...] = ()


def _git(args: list[str], cwd: Path | None = None, auth: GitAuth = NO_AUTH) -> str:
    proc = process.run(
        ["git", "-c", "safe.directory=*", *auth.config_args(), *args],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **auth.env()},
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc.stdout.strip()


def _local_commit(path: Path, ref: str) -> str | None:
    try:
        return _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], path)
    except RuntimeError:
        return None


def indexed_github_repositories(registry: RepositoryRegistry) -> list[Repository]:
    return [
        repository
        for _, repository in sorted(registry.repositories.items())
        if repository.index and repository.backend == GITHUB
    ]


def _fetch(path: Path, url: str, auth: GitAuth, refspec: str) -> None:
    """Fetch one refspec without writing ``FETCH_HEAD``.

    ``--update-head-ok`` admits a branch pin that names the branch a fresh clone's HEAD still
    points at, which the caller detaches from right after the fetch.
    """
    _git(
        [
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--update-head-ok",
            "--end-of-options",
            url,
            refspec,
        ],
        path,
        auth,
    )


def _pin_candidates(pin: str) -> list[str]:
    """Return the refs a pin may name, in the order git resolves a short name."""
    if pin.startswith("refs/"):
        return [pin]
    return [f"refs/{pin}", f"refs/tags/{pin}", f"refs/heads/{pin}"]


def _check_pin(pin: str) -> None:
    if pin.startswith("-"):
        raise ValueError(f"index_ref {pin!r} starts with '-'")
    try:
        _git(["check-ref-format", _pin_candidates(pin)[0]])
    except RuntimeError as exc:
        raise ValueError(f"index_ref {pin!r} is not a valid ref name") from exc


def _remote_ref(pin: str, path: Path, url: str, auth: GitAuth) -> str:
    candidates = _pin_candidates(pin)
    listed = _git(["ls-remote", "--end-of-options", url, *candidates], path, auth)
    names = {line.split("\t", 1)[1] for line in listed.splitlines() if "\t" in line}
    for candidate in candidates:
        if candidate in names:
            return candidate
    raise RuntimeError(f"index_ref {pin!r} names no tag or branch on the remote")


def _pinned_ref_commit(pin: str, path: Path, url: str, auth: GitAuth) -> str:
    """Fetch the ref a pin names into the local ref of the same name and return its commit.

    The forced refspec moves the local ref on every pass. A local ref that git would resolve
    ahead of it, such as a tag a clone brought with the name of a pinned branch, is deleted so
    that the pin resolves inside the checkout to the commit the sync checks out.
    """
    ref = _remote_ref(pin, path, url, auth)
    _fetch(path, url, auth, f"+{ref}:{ref}")
    candidates = _pin_candidates(pin)
    for shadow in candidates[: candidates.index(ref)]:
        _git(["update-ref", "-d", shadow], path)
    commit = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], path)
    if _local_commit(path, pin) != commit:
        raise RuntimeError(f"index_ref {pin!r} does not resolve to {ref} inside the checkout")
    return commit


def _target_commit(repository: Repository, path: Path, url: str, auth: GitAuth) -> str:
    """Return the commit the checkout should hold, fetching it unless a pinned SHA is present."""
    pin = repository.index_ref
    if pin is None:
        _fetch(path, url, auth, f"+refs/heads/{repository.default_branch}:{TARGET_REF}")
        return _git(["rev-parse", "--verify", f"{TARGET_REF}^{{commit}}"], path)
    _check_pin(pin)
    if not _FULL_OR_ABBREVIATED_SHA.match(pin):
        return _pinned_ref_commit(pin, path, url, auth)
    present = _local_commit(path, pin)
    if present:
        return present
    _fetch(path, url, auth, f"+{pin}:{TARGET_REF}")
    return _git(["rev-parse", "--verify", f"{TARGET_REF}^{{commit}}"], path)


def _checkout(path: Path, commit: str) -> None:
    _git(
        ["-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", "--detach", commit],
        path,
    )


def _clone_into_place(repository: Repository, path: Path, url: str, auth: GitAuth) -> None:
    staging = path.with_name(f".{path.name}.clone-{uuid.uuid4().hex[:12]}")
    try:
        _git(["clone", "--quiet", "--no-checkout", "--", url, str(staging)], auth=auth)
        _checkout(staging, _target_commit(repository, staging, url, auth))
        os.replace(staging, path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _is_empty_directory(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def sync_checkout(repository: Repository, path: Path, url: str, auth: GitAuth) -> str:
    """Bring one checkout to its target commit and return that commit."""
    if (path / ".git").is_dir():
        # The caller holds the repository's sync lock, so an index lock left here belongs to
        # an update that was interrupted and would otherwise fail every later pass.
        (path / ".git" / "index.lock").unlink(missing_ok=True)
        commit = _target_commit(repository, path, url, auth)
        if _git(["rev-parse", "HEAD"], path) != commit:
            _checkout(path, commit)
    elif not path.exists() or _is_empty_directory(path):
        for leftover in path.parent.glob(f".{path.name}.clone-*"):
            shutil.rmtree(leftover, ignore_errors=True)
        _clone_into_place(repository, path, url, auth)
    else:
        raise RuntimeError(f"{path} exists and is not a git checkout")
    return _git(["rev-parse", "HEAD"], path)


@contextmanager
def _repository_lock(root: Path, name: str) -> Iterator[None]:
    with (root / f".{name}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CheckoutLockedError(name) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _unlisted(root: Path, registry: RepositoryRegistry, targets: set[str]) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    known = targets | registry.names(LOCAL)
    return tuple(
        sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in known
        )
    )


def sync_index_checkouts(
    registry: RepositoryRegistry,
    root: Path,
    *,
    remote_url_for: Callable[[str], str],
    auth_for: Callable[[str], GitAuth],
) -> SyncReport:
    """Sync the checkout of every indexed GitHub repository under root, each independently."""
    targets = indexed_github_repositories(registry)
    synced: dict[str, str] = {}
    failed: dict[str, str] = {}
    locked: list[str] = []
    if targets:
        root.mkdir(parents=True, exist_ok=True)
    for repository in targets:
        name = repository.name
        try:
            with _repository_lock(root, name):
                synced[name] = sync_checkout(
                    repository, root / name, remote_url_for(name), auth_for(name)
                )
        except CheckoutLockedError:
            locked.append(name)
        except Exception as exc:  # noqa: BLE001
            failed[name] = str(exc)
            log.warning("could not sync the index checkout of %s: %s", name, exc)
    return SyncReport(
        synced=synced,
        failed=failed,
        locked=tuple(locked),
        unlisted=_unlisted(root, registry, {repository.name for repository in targets}),
    )


def sync_tenant_index_checkouts(settings: Settings) -> SyncReport:
    """Sync the index checkouts of the tenant's active registry under its repository root."""
    from services.integration.github.factory import build_git_auth_for, build_git_host
    from services.integration.repositories import load_repository_registry

    registry = load_repository_registry(settings)
    if not indexed_github_repositories(registry):
        return SyncReport(unlisted=_unlisted(Path(settings.repository_root), registry, set()))
    return sync_index_checkouts(
        registry,
        Path(settings.repository_root),
        remote_url_for=build_git_host(settings).source_location,
        auth_for=build_git_auth_for(settings),
    )


async def keep_index_checkouts(settings: Settings) -> None:
    """Sync the tenant's index checkouts now and then every interval, forever.

    An interval of zero or less makes the startup pass the only one. A commit change and a newly
    unlisted checkout are logged once each rather than on every pass.
    """
    commits: dict[str, str] = {}
    unlisted: set[str] = set()
    interval = settings.index_checkout_sync_seconds
    while True:
        try:
            report = await asyncio.to_thread(sync_tenant_index_checkouts, settings)
        except Exception:  # noqa: BLE001
            log.warning(
                "could not sync the index checkouts of tenant %s", settings.tenant, exc_info=True
            )
        else:
            for name, commit in report.synced.items():
                if commits.get(name) != commit:
                    log.info("index checkout of %s is at %s", name, commit[:12])
            commits.update(report.synced)
            for name in report.locked:
                log.info("skipped the index checkout of %s, which another worker holds", name)
            for name in sorted(set(report.unlisted) - unlisted):
                log.info(
                    "left %s in place, which the registry does not index as a GitHub repository",
                    name,
                )
            unlisted = set(report.unlisted)
        if interval <= 0:
            return
        await asyncio.sleep(interval)
