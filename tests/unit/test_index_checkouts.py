"""The tenant worker keeps the checkout of each indexed GitHub repository for indexing."""

from __future__ import annotations

import fcntl
import logging
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts.config import Settings
from services import process
from services.integration.github.git_auth import CREDENTIAL_ENV, NO_AUTH, GitAuth
from services.integration.repositories import RepositoryRegistry, parse_repositories
from services.orchestration import index_checkouts
from services.orchestration.index_checkouts import (
    SyncReport,
    keep_index_checkouts,
    sync_index_checkouts,
)

TOKEN = "ghs-index-checkout-secret"
IDENTITY = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class Remote:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.bare = tmp_path / f"{name}.git"
        self.seed = tmp_path / f"{name}-seed"
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(self.bare)],
            check=True,
            capture_output=True,
        )
        # GitHub serves a fetch of any reachable commit by its SHA, which a bare repository
        # refuses unless configured to.
        _git(
            ["--git-dir", str(self.bare), "config", "uploadpack.allowReachableSHA1InWant", "true"],
            self.bare,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.bare), str(self.seed)], check=True, capture_output=True
        )

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def tag(self, name: str) -> None:
        _git([*IDENTITY, "tag", "-f", "-a", name, "-m", name], self.seed)
        _git(["push", "-q", "-f", "origin", f"refs/tags/{name}"], self.seed)

    def branch(self, name: str) -> None:
        _git(["push", "-q", "-f", "origin", f"HEAD:refs/heads/{name}"], self.seed)

    def commit(self, filename: str, text: str) -> str:
        (self.seed / filename).write_text(text, encoding="utf-8")
        _git(["add", "-A"], self.seed)
        _git([*IDENTITY, "commit", "-q", "-m", filename], self.seed)
        _git(["push", "-q", "origin", "HEAD:main"], self.seed)
        return _git(["rev-parse", "HEAD"], self.seed)


def _registry(repositories: dict[str, dict[str, Any]], root: Path) -> RepositoryRegistry:
    return parse_repositories({"repositories": repositories}, root=str(root))


def _sync(
    registry: RepositoryRegistry,
    root: Path,
    urls: dict[str, str],
    auth: GitAuth = NO_AUTH,
) -> SyncReport:
    return sync_index_checkouts(
        registry, root, remote_url_for=urls.__getitem__, auth_for=lambda _repo: auth
    )


def _head(path: Path) -> str:
    return _git(["rev-parse", "HEAD"], path)


def _is_detached(path: Path) -> bool:
    return subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path).returncode != 0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "tenants" / "adp" / "repositories"


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    remote = Remote(tmp_path, "web")
    remote.commit("README.md", "first\n")
    return remote


def test_an_absent_checkout_is_cloned_detached_at_the_default_branch_tip(
    remote: Remote, root: Path
) -> None:
    tip = remote.commit("app.py", "print('v1')\n")
    registry = _registry({"web": {"backend": "github", "index": True}}, root)

    report = _sync(registry, root, {"web": remote.url})

    checkout = root / "web"
    assert report.synced == {"web": tip}
    assert report.failed == {}
    assert _head(checkout) == tip
    assert _is_detached(checkout)
    assert (checkout / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert [entry.name for entry in root.iterdir() if ".clone-" in entry.name] == []
    for path in (checkout, checkout / "app.py"):
        mode = path.stat().st_mode
        assert mode & stat.S_IRGRP and mode & stat.S_IROTH


def test_an_existing_checkout_is_fetched_to_the_new_tip(remote: Remote, root: Path) -> None:
    registry = _registry({"web": {"backend": "github", "index": True}}, root)
    _sync(registry, root, {"web": remote.url})

    tip = remote.commit("app.py", "print('v2')\n")
    report = _sync(registry, root, {"web": remote.url})

    assert report.synced == {"web": tip}
    assert _head(root / "web") == tip
    assert _is_detached(root / "web")
    assert (root / "web" / "app.py").read_text(encoding="utf-8") == "print('v2')\n"


def test_an_index_ref_pins_the_checkout(remote: Remote, root: Path) -> None:
    pinned = remote.commit("app.py", "print('pinned')\n")
    remote.commit("app.py", "print('later')\n")
    registry = _registry({"web": {"backend": "github", "index": True, "index_ref": pinned}}, root)

    report = _sync(registry, root, {"web": remote.url})

    assert report.synced == {"web": pinned}
    assert (root / "web" / "app.py").read_text(encoding="utf-8") == "print('pinned')\n"


def test_a_changed_index_ref_fetches_a_commit_the_checkout_lacks(
    remote: Remote, root: Path
) -> None:
    first = remote.commit("app.py", "print('one')\n")
    registry = _registry({"web": {"backend": "github", "index": True, "index_ref": first}}, root)
    _sync(registry, root, {"web": remote.url})

    second = remote.commit("app.py", "print('two')\n")
    moved = _registry({"web": {"backend": "github", "index": True, "index_ref": second}}, root)
    report = _sync(moved, root, {"web": remote.url})

    assert report.synced == {"web": second}
    assert (root / "web" / "app.py").read_text(encoding="utf-8") == "print('two')\n"


def _pinned(pin: str, root: Path) -> RepositoryRegistry:
    return _registry({"web": {"backend": "github", "index": True, "index_ref": pin}}, root)


def _pin_resolves_to_head(pin: str, checkout: Path) -> bool:
    return _git(["rev-parse", "--verify", f"{pin}^{{commit}}"], checkout) == _head(checkout)


def test_a_tag_pin_resolves_inside_the_checkout_to_head(remote: Remote, root: Path) -> None:
    tagged = remote.commit("app.py", "print('release')\n")
    remote.tag("v1")
    remote.commit("app.py", "print('after')\n")

    report = _sync(_pinned("v1", root), root, {"web": remote.url})

    assert report.synced == {"web": tagged}
    assert _pin_resolves_to_head("v1", root / "web")


def test_a_branch_pin_resolves_inside_the_checkout_to_head(remote: Remote, root: Path) -> None:
    released = remote.commit("app.py", "print('release')\n")
    remote.branch("release")
    remote.commit("app.py", "print('after')\n")

    report = _sync(_pinned("release", root), root, {"web": remote.url})

    assert report.synced == {"web": released}
    assert _pin_resolves_to_head("release", root / "web")
    assert _is_detached(root / "web")


@pytest.mark.parametrize("pin", ["main", "refs/heads/main"])
def test_a_pin_on_the_default_branch_resolves_inside_the_checkout_to_head(
    pin: str, remote: Remote, root: Path
) -> None:
    tip = remote.commit("app.py", "print('tip')\n")

    report = _sync(_pinned(pin, root), root, {"web": remote.url})

    assert report.synced == {"web": tip}
    assert _pin_resolves_to_head(pin, root / "web")


@pytest.mark.parametrize("kind", ["tag", "branch"])
def test_a_pin_that_moves_is_followed_to_its_new_commit(
    kind: str, remote: Remote, root: Path
) -> None:
    move = remote.tag if kind == "tag" else remote.branch
    remote.commit("app.py", "print('one')\n")
    move("stable")
    registry = _pinned("stable", root)
    _sync(registry, root, {"web": remote.url})

    moved = remote.commit("app.py", "print('two')\n")
    move("stable")
    report = _sync(registry, root, {"web": remote.url})

    assert report.synced == {"web": moved}
    assert _pin_resolves_to_head("stable", root / "web")
    assert (root / "web" / "app.py").read_text(encoding="utf-8") == "print('two')\n"


def test_a_branch_pin_is_not_shadowed_by_a_stale_tag_of_the_same_name(
    remote: Remote, root: Path
) -> None:
    remote.commit("app.py", "print('tagged')\n")
    remote.tag("release")
    _sync(_registry({"web": {"backend": "github", "index": True}}, root), root, {"web": remote.url})
    _git(["push", "-q", "origin", ":refs/tags/release"], remote.seed)
    branched = remote.commit("app.py", "print('branched')\n")
    remote.branch("release")

    report = _sync(_pinned("release", root), root, {"web": remote.url})

    assert report.synced == {"web": branched}
    assert _pin_resolves_to_head("release", root / "web")


@pytest.mark.parametrize("pin", ["-release", "bad..ref", "release:main"])
def test_a_malformed_pin_is_refused(pin: str, remote: Remote, root: Path) -> None:
    report = _sync(_pinned(pin, root), root, {"web": remote.url})

    assert set(report.failed) == {"web"}
    assert "index_ref" in report.failed["web"]
    assert not (root / "web").exists()


def test_local_and_unindexed_repositories_are_not_touched(
    tmp_path: Path, remote: Remote, root: Path
) -> None:
    local = root / "service"
    local.mkdir(parents=True)
    (local / "marker.txt").write_text("owned by repos-sync\n", encoding="utf-8")
    registry = _registry(
        {
            "service": {"backend": "local", "index": True, "path": str(tmp_path / "service")},
            "docs": {"backend": "github", "index": False},
        },
        root,
    )
    urls = {"service": remote.url, "docs": remote.url}

    report = _sync(registry, root, urls)

    assert report == SyncReport()
    assert sorted(entry.name for entry in root.iterdir()) == ["service"]
    assert [entry.name for entry in local.iterdir()] == ["marker.txt"]


def test_one_failing_repository_leaves_the_others_synced(remote: Remote, root: Path) -> None:
    registry = _registry(
        {
            "broken": {"backend": "github", "index": True},
            "web": {"backend": "github", "index": True},
        },
        root,
    )
    urls = {"broken": (root.parent / "missing.git").as_uri(), "web": remote.url}

    report = _sync(registry, root, urls)

    assert set(report.synced) == {"web"}
    assert set(report.failed) == {"broken"}
    assert not (root / "broken").exists()


def test_a_directory_that_is_not_a_checkout_is_refused(remote: Remote, root: Path) -> None:
    (root / "web").mkdir(parents=True)
    (root / "web" / "notes.txt").write_text("kept\n", encoding="utf-8")
    registry = _registry({"web": {"backend": "github", "index": True}}, root)

    report = _sync(registry, root, {"web": remote.url})

    assert set(report.failed) == {"web"}
    assert [entry.name for entry in (root / "web").iterdir()] == ["notes.txt"]


def test_a_repository_removed_from_the_registry_is_left_in_place(
    tmp_path: Path, remote: Remote, root: Path
) -> None:
    indexed = _registry({"web": {"backend": "github", "index": True}}, root)
    _sync(indexed, root, {"web": remote.url})
    head = _head(root / "web")

    other = Remote(tmp_path, "api")
    other.commit("main.go", "package main\n")
    report = _sync(
        _registry({"api": {"backend": "github", "index": True}}, root), root, {"api": other.url}
    )

    assert report.unlisted == ("web",)
    assert _head(root / "web") == head


def test_a_repository_another_worker_holds_is_skipped(remote: Remote, root: Path) -> None:
    root.mkdir(parents=True)
    registry = _registry({"web": {"backend": "github", "index": True}}, root)
    with (root / ".web.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = _sync(registry, root, {"web": remote.url})

    assert report.locked == ("web",)
    assert not (root / "web").exists()


def test_no_credential_is_persisted_or_passed_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, remote: Remote, root: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    run = process.run

    def recording_run(args: list[str], **kwargs: Any) -> Any:
        calls.append(([str(arg) for arg in args], dict(kwargs.get("env") or {})))
        return run(args, **kwargs)

    monkeypatch.setattr(index_checkouts.process, "run", recording_run)
    registry = _registry({"web": {"backend": "github", "index": True}}, root)
    auth = GitAuth(TOKEN, "https://acme.ghe")

    _sync(registry, root, {"web": remote.url}, auth)
    remote.commit("app.py", "print('v2')\n")
    report = _sync(registry, root, {"web": remote.url}, auth)

    assert set(report.synced) == {"web"}
    network = [env for argv, env in calls if {"clone", "fetch"} & set(argv)]
    assert network and all(env.get(CREDENTIAL_ENV) == TOKEN for env in network)
    assert all(TOKEN not in " ".join(argv) for argv, _ in calls)
    assert not (root / "web" / ".git" / "FETCH_HEAD").exists()
    leaked = [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and TOKEN.encode() in path.read_bytes()
    ]
    assert leaked == []
    assert TOKEN not in _git(["config", "--list", "--local"], root / "web")


async def test_an_interval_of_zero_runs_the_startup_pass_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    passes: list[Settings] = []

    def fake_sync(settings: Settings) -> SyncReport:
        passes.append(settings)
        return SyncReport(synced={"web": "a" * 40}, unlisted=("retired",))

    monkeypatch.setattr(index_checkouts, "sync_tenant_index_checkouts", fake_sync)
    settings = Settings(tenant="adp", index_checkout_sync_seconds=0)

    with caplog.at_level(logging.INFO, logger=index_checkouts.__name__):
        await keep_index_checkouts(settings)

    assert passes == [settings]
    assert "index checkout of web is at aaaaaaaaaaaa" in caplog.text
    assert "left retired in place" in caplog.text


async def test_a_failed_pass_is_logged_and_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing_sync(settings: Settings) -> SyncReport:
        raise RuntimeError("bundle unreadable")

    monkeypatch.setattr(index_checkouts, "sync_tenant_index_checkouts", failing_sync)

    await keep_index_checkouts(Settings(tenant="adp", index_checkout_sync_seconds=0))

    assert "could not sync the index checkouts of tenant adp" in caplog.text
