"""Unit tests for the repository index worker."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.repository_intelligence.indexing import worker as worker_module
from services.repository_intelligence.indexing.worker import (
    RepoIndexWorker,
    RepoTarget,
    build_worker,
)
from services.repository_intelligence.normalization.models import RepoRef


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, RepoRef, str | None]] = []

    def __call__(self, root: Path, repo: RepoRef, prior: str | None) -> None:
        self.calls.append((root, repo, prior))


def _worker(target: RepoTarget, *, resolver, reindexer) -> RepoIndexWorker:
    return RepoIndexWorker([target], commit_resolver=resolver, reindexer=reindexer, concurrency=1)


def test_poll_reindexes_changed_repo() -> None:
    recorder = _Recorder()
    target = RepoTarget(name="svc", root=Path("/repos/svc"))
    worker = _worker(target, resolver=lambda _root: "aaaaaaa", reindexer=recorder)

    submitted = worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert submitted == ["svc"]
    assert recorder.calls == [(Path("/repos/svc"), RepoRef("svc", "aaaaaaa"), None)]


def test_poll_skips_unchanged_repo() -> None:
    recorder = _Recorder()
    target = RepoTarget(name="svc", root=Path("/repos/svc"))
    worker = _worker(target, resolver=lambda _root: "aaaaaaa", reindexer=recorder)

    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    assert worker.poll_once() == []
    worker.close()

    assert len(recorder.calls) == 1


def test_poll_reindexes_on_new_commit_and_passes_prior() -> None:
    recorder = _Recorder()
    target = RepoTarget(name="svc", root=Path("/repos/svc"))
    commits = iter(["aaaaaaa", "bbbbbbb"])
    worker = _worker(target, resolver=lambda _root: next(commits), reindexer=recorder)

    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert [call[1] for call in recorder.calls] == [
        RepoRef("svc", "aaaaaaa"),
        RepoRef("svc", "bbbbbbb"),
    ]
    assert recorder.calls[1][2] == "aaaaaaa"


def test_pinned_target_indexes_once_without_resolving() -> None:
    recorder = _Recorder()

    def resolver(_root: Path) -> str:
        raise AssertionError("pinned targets must not resolve HEAD")

    target = RepoTarget(name="svc", root=Path("/repos/svc"), pin="ccccccc")
    worker = _worker(target, resolver=resolver, reindexer=recorder)

    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    assert worker.poll_once() == []
    worker.close()

    assert recorder.calls == [(Path("/repos/svc"), RepoRef("svc", "ccccccc"), None)]


def test_failed_reindex_is_retried_on_next_poll() -> None:
    attempts: list[str] = []

    def reindexer(_root: Path, repo: RepoRef, _prior: str | None) -> None:
        attempts.append(repo.commit_sha)
        if len(attempts) == 1:
            raise RuntimeError("boom")

    target = RepoTarget(name="svc", root=Path("/repos/svc"))
    worker = _worker(target, resolver=lambda _root: "aaaaaaa", reindexer=reindexer)

    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert attempts == ["aaaaaaa", "aaaaaaa"]


def test_build_worker_returns_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "get_settings", lambda: _Settings(registry={}))
    assert build_worker() is None


def test_build_worker_builds_targets_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = {"svc": ("/repos/svc", "abc1234"), "green": ("/repos/green", None)}
    monkeypatch.setattr(worker_module, "get_settings", lambda: _Settings(registry=registry))
    worker = build_worker()
    assert worker is not None
    assert worker.repos() == {"svc", "green"}


class _Settings:
    repo_index_worker_concurrency = 2

    def __init__(self, *, registry: dict[str, tuple[str, str | None]]) -> None:
        self._registry = registry

    def repo_intelligence_registry(self) -> dict[str, tuple[str, str | None]]:
        return self._registry
