"""Deterministic workspace materialization against a bare git remote."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.real.git_host import RealGitHost
from services.coordination.orphans import BRANCH, STAGING, InMemoryOrphanRegistry
from services.integration.github.credentials import build_single_identity_resolver
from services.integration.github.mediator import RepoNotAllowedError
from services.orchestration.materialization import WorkspaceMaterializer

REPO = "agentic-demo-service"
OWNER = "acme"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def remote(tmp_path) -> tuple[str, str]:
    path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(path), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    identity = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    _git(["add", "."], str(work))
    _git([*identity, "commit", "-q", "-m", "init"], str(work))
    _git(["push", "-q", "origin", "main"], str(work))
    head = _git(["rev-parse", "HEAD"], str(work))
    return str(path), head


def _materializer(remote: tuple[str, str], tmp_path) -> WorkspaceMaterializer:
    remote_path, _ = remote
    return WorkspaceMaterializer(
        registry=InMemoryOrphanRegistry(),
        allowed_repos=frozenset({REPO}),
        workspace_root=tmp_path / "workspaces",
        remote_url_for=lambda repo: remote_path,
    )


async def test_materializes_repo_at_manifest_sha_on_deterministic_branch(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)

    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )

    assert workspace.branch == agent_branch_name(TASK_ID)
    assert workspace.commit_sha == manifest_sha
    assert _git(["rev-parse", "HEAD"], workspace.path) == manifest_sha
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], workspace.path) == workspace.branch
    registry = materializer.registry
    assert isinstance(registry, InMemoryOrphanRegistry)
    kinds = sorted(a.artifact_type for a in registry.records.values())
    assert kinds == sorted([BRANCH, STAGING])


async def test_retry_reuses_the_same_branch_and_workspace(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    args = dict(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )

    first = await materializer.materialize(**args)
    second = await materializer.materialize(**args)

    assert second.path == first.path
    assert second.branch_orphan_id == first.branch_orphan_id
    assert second.workspace_orphan_id == first.workspace_orphan_id
    registry = materializer.registry
    assert isinstance(registry, InMemoryOrphanRegistry)
    assert len(registry.records) == 2
    branches = _git(["branch", "--list", "agent/*"], first.path).splitlines()
    assert len(branches) == 1


async def test_reuse_fetches_a_commit_pushed_after_the_workspace_was_created(
    remote, tmp_path
) -> None:
    remote_path, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    branch = agent_branch_name(TASK_ID)
    args = dict(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
    )

    first = await materializer.materialize(commit_sha=manifest_sha, **args)

    sidecar = tmp_path / "sidecar"
    subprocess.run(
        ["git", "clone", "-q", remote_path, str(sidecar)], check=True, capture_output=True
    )
    identity = ["-c", "user.email=sync@example.invalid", "-c", "user.name=sync"]
    _git(["checkout", "-q", "-B", branch, manifest_sha], str(sidecar))
    (sidecar / "sync.py").write_text("value = 1\n", encoding="utf-8")
    _git(["add", "."], str(sidecar))
    _git([*identity, "commit", "-q", "-m", "sync commit"], str(sidecar))
    pushed_sha = _git(["rev-parse", "HEAD"], str(sidecar))
    _git(["push", "-q", "origin", branch], str(sidecar))

    second = await materializer.materialize(commit_sha=pushed_sha, **args)

    assert second.path == first.path
    assert second.commit_sha == pushed_sha
    assert _git(["rev-parse", "HEAD"], second.path) == pushed_sha
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], second.path) == branch


async def test_reuse_resets_a_dirty_workspace_to_the_target_commit(remote, tmp_path) -> None:
    remote_path, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    branch = agent_branch_name(TASK_ID)
    args = dict(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
    )

    first = await materializer.materialize(commit_sha=manifest_sha, **args)
    (Path(first.path) / "README.md").write_text("dirty tracked edit\n", encoding="utf-8")
    (Path(first.path) / "scratch.py").write_text("stale = True\n", encoding="utf-8")

    sidecar = tmp_path / "sidecar"
    subprocess.run(
        ["git", "clone", "-q", remote_path, str(sidecar)], check=True, capture_output=True
    )
    identity = ["-c", "user.email=sync@example.invalid", "-c", "user.name=sync"]
    _git(["checkout", "-q", "-B", branch, manifest_sha], str(sidecar))
    (sidecar / "README.md").write_text("committed content\n", encoding="utf-8")
    (sidecar / "scratch.py").write_text("committed = True\n", encoding="utf-8")
    _git(["add", "."], str(sidecar))
    _git([*identity, "commit", "-q", "-m", "sync commit"], str(sidecar))
    pushed_sha = _git(["rev-parse", "HEAD"], str(sidecar))
    _git(["push", "-q", "origin", branch], str(sidecar))

    second = await materializer.materialize(commit_sha=pushed_sha, **args)

    assert second.path == first.path
    assert _git(["rev-parse", "HEAD"], second.path) == pushed_sha
    assert (Path(second.path) / "README.md").read_text(encoding="utf-8") == "committed content\n"
    assert _git(["status", "--porcelain"], second.path) == ""


async def _push_impl_commit(remote_path: str, tmp_path, branch: str, base_sha: str) -> str:
    impl = tmp_path / "impl"
    subprocess.run(
        ["git", "clone", "-q", remote_path, str(impl)], check=True, capture_output=True
    )
    identity = ["-c", "user.email=impl@example.invalid", "-c", "user.name=impl"]
    _git(["checkout", "-q", "-B", branch, base_sha], str(impl))
    (impl / "app.py").write_text("import os\n\nVALUE = 1\n", encoding="utf-8")
    _git(["add", "."], str(impl))
    _git([*identity, "commit", "-q", "-m", "impl"], str(impl))
    tip = _git(["rev-parse", "HEAD"], str(impl))
    _git(["push", "-q", "origin", branch], str(impl))
    return tip


def _blob(workspace: str, tree: str, path: str) -> str:
    return subprocess.run(
        ["git", "cat-file", "blob", f"{tree}:{path}"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _tree_paths(workspace: str, tree: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tree],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


async def test_repair_changes_survive_a_commit_materialized_at_the_branch_tip(
    remote, tmp_path
) -> None:
    remote_path, base_sha = remote
    materializer = _materializer(remote, tmp_path)
    branch = agent_branch_name(TASK_ID)
    tip = await _push_impl_commit(remote_path, tmp_path, branch, base_sha)
    args = dict(repo=REPO, task_id=TASK_ID, execution_id=EXECUTION_ID, base="main")

    repair_ws = await materializer.materialize(commit_sha=tip, **args)
    (Path(repair_ws.path) / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    commit_ws = await materializer.materialize(commit_sha=tip, **args)

    assert (Path(commit_ws.path) / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    request = await materializer.build_commit_request(commit_ws, message="repair")
    assert _blob(commit_ws.path, request.tree, "app.py") == "VALUE = 1\n"


async def test_repair_changes_are_lost_when_the_commit_materializes_at_base(
    remote, tmp_path
) -> None:
    remote_path, base_sha = remote
    materializer = _materializer(remote, tmp_path)
    branch = agent_branch_name(TASK_ID)
    tip = await _push_impl_commit(remote_path, tmp_path, branch, base_sha)
    args = dict(repo=REPO, task_id=TASK_ID, execution_id=EXECUTION_ID, base="main")

    repair_ws = await materializer.materialize(commit_sha=tip, **args)
    (Path(repair_ws.path) / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    commit_ws = await materializer.materialize(commit_sha=base_sha, **args)

    assert not (Path(commit_ws.path) / "app.py").exists()
    request = await materializer.build_commit_request(commit_ws, message="repair")
    assert "app.py" not in _tree_paths(commit_ws.path, request.tree)


async def test_refuses_a_non_allowlisted_repository(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)

    with pytest.raises(RepoNotAllowedError):
        await materializer.materialize(
            repo="not-allowlisted",
            task_id=TASK_ID,
            execution_id=EXECUTION_ID,
            base="main",
            commit_sha=manifest_sha,
        )


async def test_a_commit_request_carries_the_workspace_and_its_staged_tree(
    remote, tmp_path
) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )

    (Path(workspace.path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    (Path(workspace.path) / "README.md").write_text("hello\nworld\n", encoding="utf-8")

    request = await materializer.build_commit_request(workspace, message="Add feature")

    assert request.repo == REPO
    assert request.branch == workspace.branch
    assert request.base == "main"
    assert request.execution_id == EXECUTION_ID
    assert request.task_id == TASK_ID
    assert request.workspace == workspace.path
    assert _blob(workspace.path, request.tree, "feature.py") == "print('hi')\n"
    assert _blob(workspace.path, request.tree, "README.md") == "hello\nworld\n"


async def test_a_deleted_file_leaves_the_staged_tree(remote, tmp_path) -> None:
    """A mapping of path to text could not express a deletion, so deletions never reached a PR."""
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "README.md").unlink()

    staged = await materializer.stage(workspace)

    assert staged.changed is True
    assert "README.md" not in _tree_paths(workspace.path, staged.tree)
    assert await materializer.changed_paths(workspace) == ["README.md"]


async def test_an_untouched_workspace_stages_no_change(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )

    staged = await materializer.stage(workspace)

    assert staged.changed is False
    assert await materializer.changed_paths(workspace) == []


async def test_built_commit_request_pushes_idempotently_through_the_backend(
    remote, tmp_path
) -> None:
    remote_path, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    request = await materializer.build_commit_request(workspace, message="Add feature")

    backend = RealGitHost(
        build_single_identity_resolver(
            api_url="http://ghes.invalid/api/v3",
            owner=OWNER,
            git_base_url="",
            token="test-token",
            repos={REPO},
        ),
        remote_url_for=lambda repo: remote_path,
    )

    first = await backend.commit_and_push(request)
    second = await backend.commit_and_push(request)

    assert first.pushed is True
    assert second.replayed is True


async def test_staging_allowed_paths_keeps_an_undeclared_edit_out_of_the_tree(
    remote, tmp_path
) -> None:
    """A build tool rewriting a tracked file put it in a PR the ticket never authorized."""
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    (Path(workspace.path) / "README.md").write_text("regenerated\n", encoding="utf-8")

    staged = await materializer.stage(workspace, allowed_paths=("feature.py",))

    assert staged.changed is True
    assert _blob(workspace.path, staged.tree, "feature.py") == "print('hi')\n"
    assert _blob(workspace.path, staged.tree, "README.md") == "hello\n"
    assert staged.excluded == ("README.md",)


async def test_staging_an_undeclared_ticket_commits_every_changed_path(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    (Path(workspace.path) / "README.md").write_text("regenerated\n", encoding="utf-8")

    staged = await materializer.stage(workspace)

    assert _blob(workspace.path, staged.tree, "feature.py") == "print('hi')\n"
    assert _blob(workspace.path, staged.tree, "README.md") == "regenerated\n"
    assert staged.excluded == ()


async def test_staging_allowed_paths_covers_a_declared_directory(remote, tmp_path) -> None:
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    nested = Path(workspace.path) / "portal" / "src"
    nested.mkdir(parents=True)
    (nested / "page.tsx").write_text("export default null;\n", encoding="utf-8")
    (Path(workspace.path) / "README.md").write_text("regenerated\n", encoding="utf-8")

    staged = await materializer.stage(workspace, allowed_paths=("portal",))

    assert _blob(workspace.path, staged.tree, "portal/src/page.tsx") == "export default null;\n"
    assert _blob(workspace.path, staged.tree, "README.md") == "hello\n"


async def test_staging_a_declared_path_the_agent_never_created_is_not_fatal(
    remote, tmp_path
) -> None:
    """`git add -A -- missing` aborts, so the declared set is intersected before it reaches git."""
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "README.md").write_text("regenerated\n", encoding="utf-8")

    staged = await materializer.stage(workspace, allowed_paths=("never/created.py",))

    assert staged.changed is False
    assert staged.excluded == ("README.md",)


async def test_allowed_paths_drop_a_path_an_earlier_stage_already_indexed(
    remote, tmp_path
) -> None:
    """The implementation activity stages the same workspace before the commit activity does."""
    _, manifest_sha = remote
    materializer = _materializer(remote, tmp_path)
    workspace = await materializer.materialize(
        repo=REPO,
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        base="main",
        commit_sha=manifest_sha,
    )
    (Path(workspace.path) / "feature.py").write_text("print('hi')\n", encoding="utf-8")
    (Path(workspace.path) / "README.md").write_text("regenerated\n", encoding="utf-8")
    await materializer.stage(workspace)

    staged = await materializer.stage(workspace, allowed_paths=("feature.py",))

    assert _blob(workspace.path, staged.tree, "feature.py") == "print('hi')\n"
    assert _blob(workspace.path, staged.tree, "README.md") == "hello\n"
    assert staged.excluded == ("README.md",)
