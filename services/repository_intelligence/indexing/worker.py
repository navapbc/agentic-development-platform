"""Background worker that keeps repository indexes fresh by polling for new commits."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from scripts.config import get_settings

from services.repository_intelligence.indexing import scip_builder
from services.repository_intelligence.indexing.reindex import reindex_repo
from services.repository_intelligence.normalization.models import RepoRef

log = logging.getLogger("repo-index-worker")

_DEFAULT_INTERVAL = 30.0
_DEFAULT_CONCURRENCY = 2

CommitResolver = Callable[[Path], str]
Reindexer = Callable[[Path, RepoRef, str | None], None]


@dataclass(frozen=True)
class RepoTarget:
    name: str
    root: Path
    pin: str | None = None


def _default_reindex(root: Path, repo: RepoRef, prior: str | None) -> None:
    reindex_repo(root, repo, prior_commit=prior)


class RepoIndexWorker:
    """Poll registered repositories and reindex those whose commit has changed."""

    def __init__(
        self,
        targets: list[RepoTarget],
        *,
        commit_resolver: CommitResolver = scip_builder.resolve_commit,
        reindexer: Reindexer = _default_reindex,
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> None:
        self._targets = {target.name: target for target in targets}
        self._resolve = commit_resolver
        self._reindex = reindexer
        self._indexed: dict[str, str] = {}
        self._inflight: set[str] = set()
        self._idle = threading.Condition()
        self._pool = ThreadPoolExecutor(max_workers=max(1, concurrency))

    def repos(self) -> set[str]:
        return set(self._targets)

    def poll_once(self) -> list[str]:
        submitted: list[str] = []
        for name, target in self._targets.items():
            desired = target.pin or self._safe_resolve(target)
            if desired is None:
                continue
            with self._idle:
                if name in self._inflight or self._indexed.get(name) == desired:
                    continue
                self._inflight.add(name)
            self._pool.submit(self._run, target, desired)
            submitted.append(name)
        return submitted

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not self._inflight, timeout=timeout)

    def run_forever(
        self, *, interval: float = _DEFAULT_INTERVAL, stop: threading.Event | None = None
    ) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            self.poll_once()
            stop.wait(interval)

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _run(self, target: RepoTarget, desired: str) -> None:
        prior = self._indexed.get(target.name)
        ok = False
        try:
            self._reindex(target.root, RepoRef(target.name, desired), prior)
            ok = True
        except Exception:  # noqa: BLE001
            log.exception("reindex failed for %s at %s", target.name, desired[:7])
        finally:
            with self._idle:
                if ok:
                    self._indexed[target.name] = desired
                self._inflight.discard(target.name)
                self._idle.notify_all()

    def _safe_resolve(self, target: RepoTarget) -> str | None:
        try:
            return self._resolve(target.root)
        except Exception:  # noqa: BLE001
            log.warning("commit resolve failed for %s", target.name)
            return None


def build_worker() -> RepoIndexWorker | None:
    settings = get_settings()
    configured = settings.repo_intelligence_registry()
    if not configured:
        return None
    targets = [
        RepoTarget(name=name, root=Path(path), pin=commit)
        for name, (path, commit) in configured.items()
    ]
    return RepoIndexWorker(targets, concurrency=settings.repo_index_worker_concurrency)


def main() -> None:
    worker = build_worker()
    if worker is None:
        log.warning("no repositories configured; repo-index-worker idle")
        return
    interval = get_settings().repo_index_poll_interval_seconds
    log.info("repo-index-worker polling %d repos every %.0fs", len(worker.repos()), interval)
    worker.run_forever(interval=interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
