"""Unit tests for the repository index worker."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from services.integration.tenant_config import DirectorySource
from services.repository_intelligence.indexing import worker as worker_module
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.indexing.worker import (
    RepoIndexWorker,
    RepoTarget,
    build_worker,
    checkout_at_pin,
)
from services.repository_intelligence.normalization.models import RepoRef
from services.repository_intelligence.tenants import TenantRegistries
from tests.support import configure_repositories, hermetic_settings


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, RepoRef, str | None]] = []

    def __call__(self, target: RepoTarget, repo: RepoRef, prior: str | None) -> None:
        self.calls.append((target.root, repo, prior))


def _worker(target: RepoTarget, *, resolver, reindexer) -> RepoIndexWorker:
    return RepoIndexWorker([target], commit_resolver=resolver, reindexer=reindexer, concurrency=1)


def test_poll_reindexes_changed_repo() -> None:
    recorder = _Recorder()
    target = RepoTarget(name="svc", root=Path("/repos/svc"))
    worker = _worker(target, resolver=lambda _root: "aaaaaaa", reindexer=recorder)

    submitted = worker.poll_once()
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert submitted == [("", "svc")]
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
    checked: list[tuple[Path, str]] = []

    def resolver(_root: Path) -> str:
        raise AssertionError("pinned targets must not resolve HEAD")

    def pin_check(root: Path, pin: str) -> bool:
        checked.append((root, pin))
        return True

    target = RepoTarget(name="svc", root=Path("/repos/svc"), pin="ccccccc")
    worker = RepoIndexWorker(
        [target], commit_resolver=resolver, reindexer=recorder, concurrency=1, pin_check=pin_check
    )

    worker.poll_once()
    assert worker.wait_idle(timeout=5)
    assert worker.poll_once() == []
    worker.close()

    assert recorder.calls == [(Path("/repos/svc"), RepoRef("svc", "ccccccc"), None)]
    assert checked == [(Path("/repos/svc"), "ccccccc")]


def test_a_pinned_checkout_that_has_not_moved_is_skipped_until_a_later_poll(caplog) -> None:
    recorder = _Recorder()
    checkout_moved = False
    target = RepoTarget(name="svc", root=Path("/repos/svc"), pin="ddddddd", tenant="adp")
    worker = RepoIndexWorker(
        [target],
        commit_resolver=lambda _root: "ccccccc",
        reindexer=recorder,
        concurrency=1,
        pin_check=lambda _root, _pin: checkout_moved,
    )

    with caplog.at_level("DEBUG", logger="repo-index-worker"):
        assert worker.poll_once() == []
    checkout_moved = True
    assert worker.poll_once() == [("adp", "svc")]
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert recorder.calls == [(Path("/repos/svc"), RepoRef("svc", "ddddddd"), None)]
    assert any(
        record.levelname == "DEBUG" and "adp/svc" in record.getMessage()
        for record in caplog.records
    )


def test_a_failing_pin_check_skips_the_repository() -> None:
    recorder = _Recorder()

    def pin_check(_root: Path, _pin: str) -> bool:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

    target = RepoTarget(name="svc", root=Path("/repos/svc"), pin="ddddddd")
    worker = RepoIndexWorker(
        [target],
        commit_resolver=lambda _root: "ccccccc",
        reindexer=recorder,
        concurrency=1,
        pin_check=pin_check,
    )

    assert worker.poll_once() == []
    worker.close()
    assert recorder.calls == []


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "web"
    root.mkdir()
    _git(root, "init", "--quiet")
    for message in ("first", "second"):
        _git(
            root,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            message,
        )
    _git(root, "tag", "v2")
    second = _git(root, "rev-parse", "HEAD")
    first = _git(root, "rev-parse", "HEAD~1")
    _git(root, "checkout", "--quiet", "--detach", first)
    return root, first, second


def test_checkout_at_pin_compares_a_sha_pin_with_head(checkout) -> None:
    root, first, second = checkout

    assert checkout_at_pin(root, first)
    assert checkout_at_pin(root, first[:7].upper())
    assert not checkout_at_pin(root, second)
    assert not checkout_at_pin(root, second[:7])


def test_checkout_at_pin_resolves_a_named_pin_inside_the_checkout(checkout) -> None:
    root, _first, second = checkout

    assert not checkout_at_pin(root, "v2")
    _git(root, "checkout", "--quiet", "--detach", second)
    assert checkout_at_pin(root, "v2")
    assert not checkout_at_pin(root, "missing-tag")
    assert not checkout_at_pin(root, "--all")


def test_failed_reindex_is_retried_on_next_poll() -> None:
    attempts: list[str] = []

    def reindexer(_target: RepoTarget, repo: RepoRef, _prior: str | None) -> None:
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


def test_build_worker_returns_none_when_unconfigured(monkeypatch, tmp_path) -> None:
    configure_repositories(monkeypatch, tmp_path, "repositories:\n  svc:\n    index: false\n")
    monkeypatch.setattr(worker_module, "get_settings", hermetic_settings)
    assert build_worker() is None


def test_build_worker_builds_targets_from_registry(monkeypatch, tmp_path) -> None:
    configure_repositories(
        monkeypatch,
        tmp_path,
        "defaults:\n  index: true\nrepositories:\n  svc:\n    index_ref: abc1234\n  green: {}\n"
        "  other:\n    index: false\n",
    )
    monkeypatch.setattr(worker_module, "get_settings", hermetic_settings)
    worker = build_worker()
    assert worker is not None
    assert worker.repos() == {("adp", "svc"), ("adp", "green")}


def test_two_tenants_with_the_same_repository_are_indexed_and_recorded_separately() -> None:
    calls: list[tuple[RepoTarget, RepoRef, str | None]] = []
    adp = RepoTarget(name="web", root=Path("/tenants/adp/repositories/web"), tenant="adp")
    beta = RepoTarget(name="web", root=Path("/tenants/beta/repositories/web"), tenant="beta")
    commits = {adp.root: "aaaaaaa", beta.root: "bbbbbbb"}
    worker = RepoIndexWorker(
        [adp, beta],
        commit_resolver=lambda root: commits[root],
        reindexer=lambda target, repo, prior: calls.append((target, repo, prior)),
        concurrency=1,
    )

    assert sorted(worker.poll_once()) == [("adp", "web"), ("beta", "web")]
    assert worker.wait_idle(timeout=5)
    commits[beta.root] = "ccccccc"
    assert worker.poll_once() == [("beta", "web")]
    assert worker.wait_idle(timeout=5)
    worker.close()

    assert sorted((call[0].tenant, call[1].commit_sha, call[2]) for call in calls) == [
        ("adp", "aaaaaaa", None),
        ("beta", "bbbbbbb", None),
        ("beta", "ccccccc", "bbbbbbb"),
    ]


def test_the_target_loader_adds_a_repository_on_the_next_poll() -> None:
    recorder = _Recorder()
    first = RepoTarget(name="svc", root=Path("/repos/svc"), tenant="adp")
    added = RepoTarget(name="api", root=Path("/repos/api"), tenant="adp")
    loaded = [[first], [first, added]]
    worker = RepoIndexWorker(
        [],
        commit_resolver=lambda _root: "aaaaaaa",
        reindexer=recorder,
        concurrency=1,
        target_loader=lambda: loaded.pop(0),
    )

    assert worker.poll_once() == [("adp", "svc")]
    assert worker.wait_idle(timeout=5)
    assert worker.poll_once() == [("adp", "api")]
    assert worker.wait_idle(timeout=5)
    worker.close()


def test_the_default_reindexer_passes_the_targets_index_roots(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_reindex(root, repo, *, prior_commit=None, index_roots=None):
        seen.update(root=root, repo=repo, prior=prior_commit, index_roots=index_roots)

    roots = IndexRoots(Path("/idx/scip/beta"), Path("/idx/zoekt/beta"))
    target = RepoTarget(
        name="web", root=Path("/tenants/beta/repositories/web"), tenant="beta", index_roots=roots
    )
    monkeypatch.setattr(worker_module, "reindex_repo", fake_reindex)

    worker_module._default_reindex(target, RepoRef("web", "aaaaaaa"), "0000000")

    assert seen == {
        "root": Path("/tenants/beta/repositories/web"),
        "repo": RepoRef("web", "aaaaaaa"),
        "prior": "0000000",
        "index_roots": roots,
    }


def test_build_worker_reads_each_tenants_bundle(monkeypatch, tmp_path) -> None:
    configure_repositories(
        monkeypatch, tmp_path / "env", "repositories:\n  svc:\n    index: true\n"
    )
    adp = tmp_path / "tenants" / "adp"
    adp.mkdir(parents=True)
    (adp / "repositories.yaml").write_text(
        "config_version: 1\nrepositories:\n  svc:\n    index: true\n", encoding="utf-8"
    )
    beta = tmp_path / "tenants" / "beta"
    beta.mkdir(parents=True)
    (beta / "repositories.yaml").write_text(
        "config_version: 1\nrepositories:\n  svc:\n    index: true\n  docs: {}\n",
        encoding="utf-8",
    )
    settings = hermetic_settings(tenants="adp,beta", tenant="adp")
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    registries = TenantRegistries(
        settings,
        source_factory=lambda name: DirectorySource(tmp_path / "tenants" / name, name),
    )

    worker = build_worker(registries)

    assert worker is not None
    assert worker.repos() == {("adp", "svc"), ("beta", "svc")}
