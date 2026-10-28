"""The materialization path leaves no GitHub token at rest inside the execution workspace.

The workspace is copied whole into the agent sandbox and into the gate pods, so anything git
writes under `.git` leaves the platform's trust boundary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from services import process
from services.coordination.orphans import InMemoryOrphanRegistry
from services.integration.credentials import EnvSecretResolver
from services.integration.github.credentials import (
    GitHubCredentialResolver,
    parse_github_config,
)
from services.integration.github.git_auth import CREDENTIAL_ENV, GitAuth
from services.orchestration.branch_sync import GitBranchSynchronizer
from services.orchestration.materialization import WorkspaceMaterializer

REPO = "agentic-demo-service"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"
TOKEN = "ghs-workspace-secret"

RAW = {
    "hosts": {"acme": {"api_url": "https://acme.ghe/api/v3", "git_base_url": "https://acme.ghe"}},
    "default": {"host": "acme", "owner": "acme-org", "token_ref": "ACME_TOKEN"},
}


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[str, str]:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    identity = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    _git(["add", "-A"], str(seed))
    _git([*identity, "commit", "-q", "-m", "init"], str(seed))
    _git(["push", "-q", "origin", "main"], str(seed))
    return str(bare), _git(["rev-parse", "HEAD"], str(seed))


def _resolver() -> GitHubCredentialResolver:
    return GitHubCredentialResolver(
        parse_github_config(RAW), {REPO}, EnvSecretResolver({"ACME_TOKEN": TOKEN})
    )


def _token_bearing_files(root: Path) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if TOKEN.encode() in path.read_bytes():
            found.append(str(path.relative_to(root)))
    return found


async def test_clone_sends_the_token_through_the_environment_and_not_the_remote_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolver = _resolver()
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(args, **kwargs):
        calls.append(([str(arg) for arg in args], kwargs.get("env")))
        return process.CompletedProcess(list(args), 0, stdout="deadbeef", stderr="")

    monkeypatch.setattr(process, "run", fake_run)
    materializer = WorkspaceMaterializer(
        registry=InMemoryOrphanRegistry(),
        allowed_repos=frozenset({REPO}),
        workspace_root=tmp_path / "workspaces",
        remote_url_for=resolver.git_remote_url,
        auth_for=resolver.git_auth,
    )

    await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha="deadbeef",
    )

    clone_args, clone_env = next((args, env) for args, env in calls if "clone" in args)
    assert "https://acme.ghe/acme-org/agentic-demo-service.git" in clone_args
    assert clone_env is not None and clone_env[CREDENTIAL_ENV] == TOKEN
    assert not [arg for args, _ in calls for arg in args if TOKEN in arg]


async def test_materialized_workspace_holds_no_token_after_clone_and_fetch(
    remote: tuple[str, str], tmp_path: Path
) -> None:
    bare, head = remote
    materializer = WorkspaceMaterializer(
        registry=InMemoryOrphanRegistry(),
        allowed_repos=frozenset({REPO}),
        workspace_root=tmp_path / "workspaces",
        remote_url_for=lambda repo: bare,
        auth_for=lambda repo: GitAuth(TOKEN),
    )
    synchronizer = GitBranchSynchronizer(
        allowed_repos=frozenset({REPO}),
        remote_url_for=lambda repo: bare,
        auth_for=lambda repo: GitAuth(TOKEN),
    )

    workspace = await materializer.materialize(
        repo=REPO, task_id=TASK_ID, execution_id=EXECUTION_ID, base="main", commit_sha=head
    )
    await synchronizer.synchronize(
        repo=REPO,
        base="main",
        base_sha=head,
        branch=workspace.branch,
        workspace_path=workspace.path,
        resolved_files={},
    )

    git_dir = Path(workspace.path) / ".git"
    assert (git_dir / "FETCH_HEAD").is_file()
    assert _token_bearing_files(Path(workspace.path)) == []
    config = (git_dir / "config").read_text(encoding="utf-8")
    assert "credential" not in config
