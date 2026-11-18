import subprocess
from dataclasses import fields
from pathlib import Path

import pytest
from adapters.interfaces.git_host import CommitRequest, GitHost, PublishResult
from adapters.mock.git_host import MockGitHost
from adapters.real.git_host import RealGitHost
from adapters.real.local_git_host import LocalGitHost
from adapters.real.routing_git_host import RoutingGitHost
from services.integration.github.factory import GITHUB, LOCAL, backend_for, build_git_host
from tests.support import configure_repositories, hermetic_settings


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    repo = root / "demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "user.email", "tester@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return root


def _staged_request(
    root: Path, tmp_path: Path, *, branch: str, task_id: str, execution_id: str, files: dict
) -> CommitRequest:
    """A request whose workspace holds the change, as the materializer produces one."""
    workspace = tmp_path / "workspaces" / execution_id
    _git(tmp_path, "clone", "-q", str(root / "demo"), str(workspace))
    for name, text in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(workspace, "add", "-A")
    return CommitRequest(
        repo="demo",
        branch=branch,
        base="main",
        message="add feature",
        workspace=str(workspace),
        tree=_git(workspace, "write-tree"),
        task_id=task_id,
        execution_id=execution_id,
    )


def test_each_repository_resolves_its_configured_backend(monkeypatch, tmp_path) -> None:
    configure_repositories(
        monkeypatch,
        tmp_path / "config",
        "defaults:\n  backend: local\nrepositories:\n  demo:\n    path: /srv/demo\n"
        "  svc:\n    backend: github\n",
    )
    settings = hermetic_settings()

    assert backend_for("demo", settings) == LOCAL
    assert backend_for("svc", settings) == GITHUB


def test_the_git_host_routes_each_repository_to_its_backend(
    monkeypatch, tmp_path, local_repo
) -> None:
    root = local_repo
    configure_repositories(
        monkeypatch,
        tmp_path / "config",
        f"repositories:\n  demo:\n    backend: local\n    path: {root / 'demo'}\n  svc: {{}}\n",
    )
    sentinel = object()
    monkeypatch.setattr(
        "adapters.real.git_host.RealGitHost.from_settings",
        classmethod(lambda cls, settings=None: sentinel),
    )
    host = build_git_host(hermetic_settings(repository_root=str(root)))

    assert isinstance(host, RoutingGitHost)
    assert isinstance(host.host_for("demo"), LocalGitHost)
    assert host.host_for("svc") is sentinel


def test_local_host_conforms_to_git_host(local_repo) -> None:
    root = local_repo
    host: GitHost = LocalGitHost(root)
    assert host is not None


@pytest.mark.asyncio
async def test_commit_lands_on_local_branch_without_push(local_repo, tmp_path: Path) -> None:
    root = local_repo
    repo = root / "demo"
    host = LocalGitHost(root)

    await host.ensure_branch("demo", "agent/T-1", "main")
    request = _staged_request(
        root,
        tmp_path,
        branch="agent/T-1",
        task_id="T-1",
        execution_id="E-1",
        files={"feature.txt": "hello\n"},
    )

    result = await host.commit_and_push(request)

    assert result.pushed is False
    assert result.replayed is False
    assert result.published_tree == request.tree
    committed = _git(repo, "show", "agent/T-1:feature.txt")
    assert committed == "hello"
    assert _git(repo, "remote").strip() == ""
    assert _git(repo, "show", "main:README.md") == "base"


@pytest.mark.asyncio
async def test_commit_is_idempotent_on_replay(local_repo, tmp_path: Path) -> None:
    root = local_repo
    host = LocalGitHost(root)
    request = _staged_request(
        root,
        tmp_path,
        branch="agent/T-2",
        task_id="T-2",
        execution_id="E-2",
        files={"feature.txt": "hello\n"},
    )
    first = await host.commit_and_push(request)
    second = await host.commit_and_push(request)
    assert second.replayed is True
    assert second.commit_sha == first.commit_sha


@pytest.mark.asyncio
async def test_publish_writes_artifacts_and_opens_no_remote_pr(local_repo, tmp_path: Path) -> None:
    root = local_repo
    repo = root / "demo"
    host = LocalGitHost(root)
    await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-3",
            task_id="T-3",
            execution_id="E-3",
            files={"feature.txt": "hello\n"},
        )
    )

    pr = await host.open_draft_pr("demo", "agent/T-3", "main", "Add feature", "body text")

    assert pr.draft is True
    assert pr.state == "open"
    summary = _git(repo, "show", "refs/agent-results/agent/T-3:SUMMARY.md")
    patch = _git(repo, "show", "refs/agent-results/agent/T-3:change.patch")
    assert "Add feature" in summary
    assert "feature.txt" in patch
    assert _git(repo, "remote").strip() == ""
    assert _git(repo, "status", "--porcelain").strip() == ""

    found = await host.find_open_pr("demo", "agent/T-3")
    assert found is not None and found.number == pr.number

    ready = await host.mark_pr_ready("demo", pr.number)
    assert ready.draft is False

    closed = await host.close_pr("demo", pr.number)
    assert closed.state == "closed"
    assert await host.find_open_pr("demo", "agent/T-3") is None
    assert _git(repo, "show", "refs/agent-results/agent/T-3:change.patch") == patch


@pytest.mark.asyncio
async def test_local_publish_fills_result_with_branch_and_patch(local_repo, tmp_path: Path) -> None:
    root = local_repo
    host = LocalGitHost(root)
    await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-4",
            task_id="T-4",
            execution_id="E-4",
            files={"feature.txt": "hello\n"},
        )
    )

    result = await host.publish("demo", "agent/T-4", "main", "Add feature", "body")

    assert isinstance(result, PublishResult)
    assert result.branch == "agent/T-4"
    assert result.completion_signal_id != ""
    assert result.patch_path == "refs/agent-results/agent/T-4:change.patch"
    assert "feature.txt" in (await host.read_result("demo", "agent/T-4", "change.patch") or "")


@pytest.mark.asyncio
async def test_local_discovery_finds_open_branch_then_none_after_close(
    local_repo, tmp_path: Path
) -> None:
    root = local_repo
    host = LocalGitHost(root)
    await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-5",
            task_id="T-5",
            execution_id="E-5",
            files={"feature.txt": "hello\n"},
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


async def test_publish_after_the_base_moved_keeps_the_patch_to_the_change(
    local_repo, tmp_path: Path
) -> None:
    root = local_repo
    repo = root / "demo"
    host = LocalGitHost(root)
    await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-6",
            task_id="T-6",
            execution_id="E-6a",
            files={"feature.txt": "first run\n"},
        )
    )
    (repo / "notes.md").write_text("landed on main\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "advance base")

    await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-6",
            task_id="T-6",
            execution_id="E-6b",
            files={"feature.txt": "second run\n"},
        )
    )
    result = await host.publish("demo", "agent/T-6", "main", "Add feature", "body")

    patch = _git(repo, "show", result.patch_path)
    assert "feature.txt" in patch
    assert "notes.md" not in patch


async def test_rerun_on_an_unchanged_base_keeps_a_single_parent(local_repo, tmp_path: Path) -> None:
    root = local_repo
    repo = root / "demo"
    host = LocalGitHost(root)
    first = await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-7",
            task_id="T-7",
            execution_id="E-7a",
            files={"feature.txt": "first run\n"},
        )
    )

    second = await host.commit_and_push(
        _staged_request(
            root,
            tmp_path,
            branch="agent/T-7",
            task_id="T-7",
            execution_id="E-7b",
            files={"feature.txt": "second run\n"},
        )
    )

    parents = _git(repo, "rev-list", "--parents", "-n", "1", second.commit_sha).split()[1:]
    assert parents == [first.commit_sha]


def test_the_git_host_falls_back_to_no_credential_for_an_unconfigured_repository() -> None:
    from services.integration.github.credentials import build_single_identity_resolver
    from services.integration.github.git_auth import NO_AUTH

    host = RealGitHost(
        build_single_identity_resolver(
            api_url="https://ghe.invalid/api/v3",
            owner="acme",
            git_base_url="https://ghe.invalid",
            token="",
            repos={"demo"},
        ),
        remote_url_for=lambda repo: "/srv/git/demo.git",
    )

    assert host._auth("demo") == NO_AUTH


def test_the_git_host_still_reports_a_configured_repository_whose_token_is_missing() -> None:
    from services.integration.credentials import EnvSecretResolver, SecretNotFoundError
    from services.integration.github.credentials import (
        GitHubCredentialResolver,
        parse_github_config,
    )

    config = parse_github_config(
        {
            "hosts": {
                "acme": {
                    "api_url": "https://ghe.invalid/api/v3",
                    "git_base_url": "https://ghe.invalid",
                }
            },
            "default": {"host": "acme", "owner": "acme", "token_ref": "ACME_TOKEN"},
        }
    )
    host = RealGitHost(
        GitHubCredentialResolver(config, {"demo"}, EnvSecretResolver({})),
        remote_url_for=lambda repo: "/srv/git/demo.git",
    )

    with pytest.raises(SecretNotFoundError):
        host._auth("demo")
