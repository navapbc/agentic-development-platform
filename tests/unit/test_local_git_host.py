import subprocess
from dataclasses import fields
from pathlib import Path

import pytest
from adapters.interfaces.git_host import CommitRequest, GitHost, PublishResult
from adapters.mock.git_host import MockGitHost
from adapters.real.git_host import RealGitHost
from adapters.real.local_git_host import LocalGitHost
from scripts.config import Settings
from services.integration.credentials import ConfigError
from services.integration.github.factory import (
    GITHUB,
    LOCAL,
    build_git_host,
    resolve_source_control_backend_name,
)
from tests.support import hermetic_settings


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def local_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repos"
    repo = root / "demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "user.email", "tester@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return root, tmp_path / "artifacts"


def test_default_settings_resolve_github() -> None:
    assert resolve_source_control_backend_name(hermetic_settings()) == GITHUB


def test_unset_backend_resolves_github() -> None:
    assert resolve_source_control_backend_name(Settings(source_control_backend="")) == GITHUB


def test_backend_name_is_case_insensitive() -> None:
    assert resolve_source_control_backend_name(Settings(source_control_backend="Local")) == LOCAL


def test_unknown_backend_fails_closed() -> None:
    with pytest.raises(ConfigError, match="unknown source_control_backend"):
        resolve_source_control_backend_name(Settings(source_control_backend="gitlab"))


def test_default_build_resolves_real_git_host(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(
        "adapters.real.git_host.RealGitHost.from_settings",
        classmethod(lambda cls, settings=None: sentinel),
    )
    assert build_git_host(hermetic_settings()) is sentinel


def test_local_build_resolves_local_git_host(local_repo) -> None:
    root, artifacts = local_repo
    host = build_git_host(
        Settings(
            source_control_backend="local",
            source_control_local_root=str(root),
            source_control_local_artifacts_root=str(artifacts),
        )
    )
    assert isinstance(host, LocalGitHost)


def test_local_host_conforms_to_git_host(local_repo) -> None:
    root, artifacts = local_repo
    host: GitHost = LocalGitHost(root, artifacts_root=artifacts)
    assert host is not None


@pytest.mark.asyncio
async def test_commit_lands_on_local_branch_without_push(local_repo) -> None:
    root, artifacts = local_repo
    repo = root / "demo"
    host = LocalGitHost(root, artifacts_root=artifacts)

    await host.ensure_branch("demo", "agent/T-1", "main")
    result = await host.commit_and_push(
        CommitRequest(
            repo="demo",
            branch="agent/T-1",
            base="main",
            message="add feature",
            files={"feature.txt": "hello\n"},
            task_id="T-1",
            execution_id="E-1",
        )
    )

    assert result.pushed is False
    assert result.replayed is False
    committed = _git(repo, "show", "agent/T-1:feature.txt")
    assert committed == "hello"
    assert _git(repo, "remote").strip() == ""
    assert _git(repo, "show", "main:README.md") == "base"


@pytest.mark.asyncio
async def test_commit_is_idempotent_on_replay(local_repo) -> None:
    root, artifacts = local_repo
    host = LocalGitHost(root, artifacts_root=artifacts)
    request = CommitRequest(
        repo="demo",
        branch="agent/T-2",
        base="main",
        message="add feature",
        files={"feature.txt": "hello\n"},
        task_id="T-2",
        execution_id="E-2",
    )
    first = await host.commit_and_push(request)
    second = await host.commit_and_push(request)
    assert second.replayed is True
    assert second.commit_sha == first.commit_sha


@pytest.mark.asyncio
async def test_publish_writes_artifacts_and_opens_no_remote_pr(local_repo) -> None:
    root, artifacts = local_repo
    repo = root / "demo"
    host = LocalGitHost(root, artifacts_root=artifacts)
    await host.commit_and_push(
        CommitRequest(
            repo="demo",
            branch="agent/T-3",
            base="main",
            message="add feature",
            files={"feature.txt": "hello\n"},
            task_id="T-3",
            execution_id="E-3",
        )
    )

    pr = await host.open_draft_pr("demo", "agent/T-3", "main", "Add feature", "body text")

    assert pr.draft is True
    assert pr.state == "open"
    summary = artifacts / "demo" / "agent/T-3" / "SUMMARY.md"
    patch = artifacts / "demo" / "agent/T-3" / "change.patch"
    assert "Add feature" in summary.read_text(encoding="utf-8")
    assert "feature.txt" in patch.read_text(encoding="utf-8")
    assert _git(repo, "remote").strip() == ""

    found = await host.find_open_pr("demo", "agent/T-3")
    assert found is not None and found.number == pr.number

    ready = await host.mark_pr_ready("demo", pr.number)
    assert ready.draft is False

    closed = await host.close_pr("demo", pr.number)
    assert closed.state == "closed"
    assert await host.find_open_pr("demo", "agent/T-3") is None


@pytest.mark.asyncio
async def test_local_publish_fills_result_with_branch_and_patch(local_repo) -> None:
    root, artifacts = local_repo
    host = LocalGitHost(root, artifacts_root=artifacts)
    await host.commit_and_push(
        CommitRequest(
            repo="demo",
            branch="agent/T-4",
            base="main",
            message="add feature",
            files={"feature.txt": "hello\n"},
            task_id="T-4",
            execution_id="E-4",
        )
    )

    result = await host.publish("demo", "agent/T-4", "main", "Add feature", "body")

    assert isinstance(result, PublishResult)
    assert result.branch == "agent/T-4"
    assert result.completion_signal_id != ""
    assert Path(result.patch_path).exists()
    assert "feature.txt" in Path(result.patch_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_local_discovery_finds_open_branch_then_none_after_close(local_repo) -> None:
    root, artifacts = local_repo
    host = LocalGitHost(root, artifacts_root=artifacts)
    await host.commit_and_push(
        CommitRequest(
            repo="demo",
            branch="agent/T-5",
            base="main",
            message="add feature",
            files={"feature.txt": "hello\n"},
            task_id="T-5",
            execution_id="E-5",
        )
    )
    pr = await host.publish("demo", "agent/T-5", "main", "Add feature", "body")

    found = await host.find_published_branch("demo", "agent/T-5")
    assert found is not None
    assert found.reused is True
    assert found.completion_signal_id == pr.completion_signal_id

    await host.close_pr("demo", int(pr.completion_signal_id))
    assert await host.find_published_branch("demo", "agent/T-5") is None


@pytest.mark.asyncio
async def test_pr_backed_publish_uses_pr_number() -> None:
    host = MockGitHost()
    host.seed_repo("demo", files={"README.md": "base\n"})
    await host.ensure_branch("demo", "agent/T-6", "main")

    result = await host.publish("demo", "agent/T-6", "main", "Add feature", "body")

    assert result.branch == "agent/T-6"
    assert result.completion_signal_id == "1"
    assert result.summary_path == ""
    assert result.patch_path == ""


@pytest.mark.asyncio
async def test_pr_backed_discovery_finds_draft_branch() -> None:
    host = MockGitHost()
    host.seed_repo("demo", files={"README.md": "base\n"})
    await host.ensure_branch("demo", "agent/T-7", "main")
    await host.publish("demo", "agent/T-7", "main", "Add feature", "body")

    found = await host.find_published_branch("demo", "agent/T-7")
    assert found is not None
    assert found.reused is True
    assert await host.find_published_branch("demo", "agent/absent") is None


@pytest.mark.asyncio
async def test_real_host_publish_uses_pr_number(monkeypatch) -> None:
    from adapters.interfaces.git_host import PullRequest

    host = RealGitHost.__new__(RealGitHost)

    async def fake_open(repo, head, base, title, body):
        return PullRequest(
            repo=repo,
            number=42,
            url=f"https://git.invalid/{repo}/pull/42",
            head=head,
            base=base,
            title=title,
            body=body,
            draft=True,
            state="open",
            created=True,
        )

    monkeypatch.setattr(host, "open_draft_pr", fake_open)

    result = await host.publish("demo", "agent/T-8", "main", "Add feature", "body")

    assert result.completion_signal_id == "42"
    assert result.branch == "agent/T-8"
    assert result.created is True


def test_publish_result_carries_no_approval_or_status_field() -> None:
    names = {f.name for f in fields(PublishResult)}
    assert "approved" not in names
    assert "approver" not in names
    assert "status" not in names
    assert names == {
        "repo",
        "branch",
        "completion_signal_id",
        "summary_path",
        "patch_path",
        "created",
        "reused",
    }
