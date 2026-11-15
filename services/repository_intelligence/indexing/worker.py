"""Background worker that keeps repository indexes fresh by polling for new commits.

The worker indexes every repository a served tenant marks for indexing, keyed by tenant and
repository, and reloads the tenant registries before each poll so a bundle change takes effect.

A pinned repository is indexed only once the HEAD of its checkout resolves to the pin. A bundle
can name a new ``index_ref`` before the checkout has moved, and indexing at that moment would
record the files of the previous commit under the new pin. The worker skips such a repository
and checks it again on a later poll.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from scripts.config import configured_log_level, get_settings

from services import process
from services.healthcheck import touch
from services.repository_intelligence.indexing import scip_builder
from services.repository_intelligence.indexing.reindex import IndexRoots, reindex_repo
from services.repository_intelligence.normalization.models import RepoRef
from services.repository_intelligence.tenants import TenantRegistries, migrate_home_indexes

log = logging.getLogger("repo-index-worker")

_DEFAULT_INTERVAL = 30.0
_DEFAULT_CONCURRENCY = 2
_FULL_OR_ABBREVIATED_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")

RepoKey = tuple[str, str]


@dataclass(frozen=True)
class RepoTarget:
    name: str
    root: Path
    pin: str | None = None
    tenant: str = ""
    index_roots: IndexRoots | None = None

    @property
    def key(self) -> RepoKey:
        return (self.tenant, self.name)

    @property
    def label(self) -> str:
        return f"{self.tenant}/{self.name}" if self.tenant else self.name


CommitResolver = Callable[[Path], str]
Reindexer = Callable[[RepoTarget, RepoRef, str | None], None]
TargetLoader = Callable[[], list[RepoTarget]]
PinCheck = Callable[[Path, str], bool]


def checkout_at_pin(root: Path, pin: str) -> bool:
    """Return whether the HEAD of the checkout at root resolves to the commit the pin names.

    A full or abbreviated SHA is compared with HEAD directly. Any other pin must resolve to the
    HEAD commit inside the checkout itself.
    """
    head = scip_builder.resolve_commit(root).lower()
    if _FULL_OR_ABBREVIATED_SHA.match(pin):
        return head.startswith(pin.lower())
    if pin.startswith("-"):
        return False
    resolved = process.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{pin}^{{commit}}"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return resolved.returncode == 0 and resolved.stdout.strip().lower() == head


def _default_reindex(target: RepoTarget, repo: RepoRef, prior: str | None) -> None:
    reindex_repo(target.root, repo, prior_commit=prior, index_roots=target.index_roots)


class RepoIndexWorker:
    """Poll registered repositories and reindex those whose commit has changed."""

    def __init__(
        self,
        targets: list[RepoTarget],
        *,
        commit_resolver: CommitResolver = scip_builder.resolve_commit,
        reindexer: Reindexer = _default_reindex,
        concurrency: int = _DEFAULT_CONCURRENCY,
        target_loader: TargetLoader | None = None,
        pin_check: PinCheck = checkout_at_pin,
    ) -> None:
        self._targets = {target.key: target for target in targets}
        self._resolve = commit_resolver
        self._reindex = reindexer
        self._load_targets = target_loader
        self._pin_check = pin_check
        self._indexed: dict[RepoKey, str] = {}
        self._inflight: set[RepoKey] = set()
        self._idle = threading.Condition()
        self._pool = ThreadPoolExecutor(max_workers=max(1, concurrency))

    def repos(self) -> set[RepoKey]:
        return set(self._targets)

    def poll_once(self) -> list[RepoKey]:
        if self._load_targets is not None:
            self._targets = {target.key: target for target in self._load_targets()}
        submitted: list[RepoKey] = []
        for key, target in self._targets.items():
            desired = target.pin or self._safe_resolve(target)
            if desired is None:
                continue
            with self._idle:
                if key in self._inflight or self._indexed.get(key) == desired:
                    continue
            if target.pin is not None and not self._checkout_reached_pin(target, target.pin):
                continue
            with self._idle:
                self._inflight.add(key)
            self._pool.submit(self._run, target, desired)
            submitted.append(key)
        return submitted

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not self._inflight, timeout=timeout)

    def run_forever(
        self,
        *,
        interval: float = _DEFAULT_INTERVAL,
        stop: threading.Event | None = None,
        heartbeat_file: Path | None = None,
    ) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            self.poll_once()
            if heartbeat_file is not None:
                touch(heartbeat_file)
            stop.wait(interval)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _run(self, target: RepoTarget, desired: str) -> None:
        prior = self._indexed.get(target.key)
        ok = False
        try:
            self._reindex(target, RepoRef(target.name, desired), prior)
            ok = True
        except Exception:  # noqa: BLE001
            log.exception("reindex failed for %s at %s", target.label, desired[:7])
        finally:
            with self._idle:
                if ok:
                    self._indexed[target.key] = desired
                self._inflight.discard(target.key)
                self._idle.notify_all()

    def _checkout_reached_pin(self, target: RepoTarget, pin: str) -> bool:
        try:
            reached = self._pin_check(target.root, pin)
        except Exception:  # noqa: BLE001
            reached = False
        if not reached:
            log.debug("checkout of %s is not at pin %s yet, skipping", target.label, pin)
        return reached

    def _safe_resolve(self, target: RepoTarget) -> str | None:
        try:
            return self._resolve(target.root)
        except Exception:  # noqa: BLE001
            log.warning("commit resolve failed for %s", target.label)
            return None


def tenant_targets(registries: TenantRegistries) -> list[RepoTarget]:
    """Return a target for every repository a served tenant indexes."""
    return [
        RepoTarget(
            name=entry.name,
            root=entry.root,
            pin=entry.ref,
            tenant=entry.tenant,
            index_roots=entry.index_roots,
        )
        for entry in registries.indexed()
    ]


def build_worker(registries: TenantRegistries | None = None) -> RepoIndexWorker | None:
    settings = get_settings()
    resolved = registries or TenantRegistries(settings)
    targets = tenant_targets(resolved)
    if not targets:
        return None
    return RepoIndexWorker(
        targets,
        concurrency=settings.repo_index_worker_concurrency,
        target_loader=lambda: tenant_targets(resolved),
    )


def main() -> None:
    settings = get_settings()
    registries = TenantRegistries(settings)
    migrate_home_indexes(registries, settings)
    worker = build_worker(registries)
    if worker is None:
        log.warning("no repositories configured, so repo-index-worker idles until one is")
        worker = RepoIndexWorker(
            [],
            concurrency=settings.repo_index_worker_concurrency,
            target_loader=lambda: tenant_targets(registries),
        )
    interval = settings.repo_index_poll_interval_seconds
    log.info("repo-index-worker polling %d repos every %.0fs", len(worker.repos()), interval)
    heartbeat_file = (
        Path(settings.health_heartbeat_file) if settings.health_heartbeat_file else None
    )
    worker.run_forever(interval=interval, heartbeat_file=heartbeat_file)


if __name__ == "__main__":
    logging.basicConfig(level=configured_log_level())
    main()
