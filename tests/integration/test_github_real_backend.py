"""Real GHES backend git mechanics against a bare remote and PRs against a fake transport."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import httpx
import pytest
from adapters.interfaces.git_host import CommitRequest, agent_branch_name
from adapters.real.git_host import RealGitHost
from services.integration.github.credentials import build_single_identity_resolver

REPO = "agentic-demo-service"
OWNER = "acme"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_remote(tmp_path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    identity = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    _git(["add", "."], str(work))
    _git([*identity, "commit", "-q", "-m", "init"], str(work))
    _git(["push", "-q", "origin", "main"], str(work))
    return str(remote)


def _fake_ghes() -> httpx.MockTransport:
    state: dict[str, list[dict]] = {"pulls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/pulls"):
            body = json.loads(request.content)
            number = len(state["pulls"]) + 1
            pull = {
                "number": number,
                "node_id": f"PR_{number}",
                "html_url": f"https://ghes.invalid/pr/{number}",
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "title": body["title"],
                "body": body.get("body", ""),
                "draft": body.get("draft", True),
                "state": "open",
            }
            state["pulls"].append(pull)
            return httpx.Response(201, json=pull)
        if request.method == "POST" and path.endswith("/graphql"):
            variables = json.loads(request.content)["variables"]
            node_id = variables["id"]
            pull = next(p for p in state["pulls"] if p["node_id"] == node_id)
            pull["draft"] = False
            return httpx.Response(
                200,
                json={
                    "data": {
                        "markPullRequestReadyForReview": {"pullRequest": {"number": pull["number"]}}
                    }
                },
            )
        if request.method == "PATCH" and "/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1])
            body = json.loads(request.content)
            pull = next(p for p in state["pulls"] if p["number"] == number)
            pull.update({k: v for k, v in body.items() if k in ("title", "body")})
            return httpx.Response(200, json=pull)
        if request.method == "GET" and path.endswith("/pulls"):
            head = request.url.params.get("head")
            matches = [
                p
                for p in state["pulls"]
                if p["state"] == "open" and (head is None or f"{OWNER}:{p['head']['ref']}" == head)
            ]
            return httpx.Response(200, json=matches)
        if request.method == "GET" and "/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1])
            pull = next(p for p in state["pulls"] if p["number"] == number)
            return httpx.Response(200, json=pull)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _host(bare_remote: str) -> RealGitHost:
    return RealGitHost(
        build_single_identity_resolver(
            api_url="http://ghes.invalid/api/v3",
            owner=OWNER,
            git_base_url="",
            token="test-token",
            repos={REPO},
        ),
        remote_url_for=lambda repo: bare_remote,
        transport=_fake_ghes(),
    )


async def test_ensure_branch_creates_then_is_a_noop(bare_remote: str) -> None:
    host = _host(bare_remote)
    name = agent_branch_name(TASK_ID)

    created = await host.ensure_branch(REPO, name, "main")
    assert created.created is True

    again = await host.ensure_branch(REPO, name, "main")
    assert again.created is False
    assert again.head_sha == created.head_sha


def _capture(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _workspace(tmp_path, bare_remote: str, name: str, ref: str = "main") -> str:
    work = tmp_path / "workspaces" / name
    work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", "--branch", ref, bare_remote, str(work)],
        check=True,
        capture_output=True,
    )
    return str(work)


def _stage(workspace: str, execution_id: str = EXECUTION_ID) -> CommitRequest:
    _git(["add", "-A"], workspace)
    return CommitRequest(
        repo=REPO,
        branch=agent_branch_name(TASK_ID),
        base="main",
        message="apply change",
        workspace=workspace,
        tree=_capture(workspace, "write-tree"),
        task_id=TASK_ID,
        execution_id=execution_id,
    )


def _write(workspace: str, path: str, text: str) -> None:
    target = pathlib.Path(workspace) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def test_a_commit_publishes_the_tree_the_workspace_staged(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    work = _workspace(tmp_path, bare_remote, "one")
    _write(work, "src/app.py", "print('hi')\n")
    request = _stage(work)

    result = await host.commit_and_push(request)

    assert result.pushed is True
    assert result.replayed is False
    assert result.published_tree == request.tree
    assert _capture(work, "ls-remote", bare_remote, request.branch).split()[0] == result.commit_sha


async def test_a_retry_of_the_same_tree_replays_instead_of_pushing(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    work = _workspace(tmp_path, bare_remote, "one")
    _write(work, "src/app.py", "print('hi')\n")
    request = _stage(work)

    first = await host.commit_and_push(request)
    retry = await host.commit_and_push(request)

    assert retry.replayed is True
    assert retry.pushed is False
    assert retry.commit_sha == first.commit_sha


async def test_a_second_change_in_one_execution_publishes_a_second_commit(
    bare_remote: str, tmp_path
) -> None:
    """The repair loop publishes several commits under one execution id."""
    host = _host(bare_remote)
    work = _workspace(tmp_path, bare_remote, "one")
    _write(work, "src/app.py", "print('hi')\n")
    first = await host.commit_and_push(_stage(work))

    _write(work, "src/app.py", "print('repaired')\n")
    second = await host.commit_and_push(_stage(work))

    assert second.pushed is True
    assert second.commit_sha != first.commit_sha
    assert _capture(work, "show", f"{second.commit_sha}:src/app.py") == "print('repaired')"


async def test_a_deletion_reaches_the_published_tree(bare_remote: str, tmp_path) -> None:
    host = _host(bare_remote)
    work = _workspace(tmp_path, bare_remote, "one")
    (pathlib.Path(work) / "README.md").unlink()

    result = await host.commit_and_push(_stage(work))

    assert result.published_tree == _capture(work, "rev-parse", f"{result.commit_sha}^{{tree}}")
    listing = _capture(work, "ls-tree", "--name-only", result.commit_sha)
    assert "README.md" not in listing.split()


async def test_bytes_that_are_not_utf8_reach_the_published_tree(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    work = _workspace(tmp_path, bare_remote, "one")
    payload = b"\x89PNG\r\n\x1a\n\xff\xfe"
    (pathlib.Path(work) / "logo.png").write_bytes(payload)

    result = await host.commit_and_push(_stage(work))

    blob = subprocess.run(
        ["git", "cat-file", "blob", f"{result.commit_sha}:logo.png"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    assert blob.stdout == payload


async def test_a_workspace_that_does_not_descend_from_the_branch_still_publishes(
    bare_remote: str, tmp_path
) -> None:
    """A rerun materializes at the base while the branch survives from the previous run."""
    host = _host(bare_remote)
    earlier = _workspace(tmp_path, bare_remote, "earlier")
    _write(earlier, "src/app.py", "print('first run')\n")
    previous = await host.commit_and_push(_stage(earlier, "exec-1"))

    rerun = _workspace(tmp_path, bare_remote, "rerun")
    _write(rerun, "src/app.py", "print('second run')\n")
    request = _stage(rerun, "exec-2")
    result = await host.commit_and_push(request)

    assert result.pushed is True
    assert result.published_tree == request.tree
    assert _capture(rerun, "rev-parse", f"{result.commit_sha}^") == previous.commit_sha


async def test_open_draft_pr_creates_then_updates_the_same_pr(bare_remote: str) -> None:
    host = _host(bare_remote)
    branch = agent_branch_name(TASK_ID)

    created = await host.open_draft_pr(REPO, branch, "main", "Draft", "body")
    assert created.created is True
    assert created.draft is True

    updated = await host.open_draft_pr(REPO, branch, "main", "Draft v2", "body v2")
    assert updated.number == created.number
    assert updated.created is False
    assert updated.title == "Draft v2"


async def test_mark_pr_ready_flips_draft_and_is_idempotent(bare_remote: str) -> None:
    host = _host(bare_remote)
    branch = agent_branch_name(TASK_ID)

    created = await host.open_draft_pr(REPO, branch, "main", "Draft", "body")
    assert created.draft is True

    ready = await host.mark_pr_ready(REPO, created.number)
    assert ready.number == created.number
    assert ready.draft is False
    assert ready.state == "open"

    again = await host.mark_pr_ready(REPO, created.number)
    assert again.draft is False


@pytest.mark.skipif(
    not os.getenv("GITHUB_LIVE_SMOKE"),
    reason="requires VPN and a GHES token; set GITHUB_LIVE_SMOKE to run",
)
async def test_live_smoke_against_an_allowlisted_demo_repo(tmp_path) -> None:
    host = RealGitHost.from_settings()
    name = agent_branch_name(TASK_ID)
    branch = await host.ensure_branch(REPO, name, "main")
    assert branch.name == name
    work = _workspace(tmp_path, host.source_location(REPO), "smoke")
    _write(work, "AGENT_SMOKE.md", "smoke\n")
    request = _stage(work)

    commit = await host.commit_and_push(request)

    assert commit.commit_sha
    assert commit.published_tree == request.tree
    pull = await host.open_draft_pr(REPO, name, "main", "Live smoke", "live smoke")
    assert pull.number


def _advance_base(tmp_path, bare_remote: str, path: str, text: str) -> str:
    work = _workspace(tmp_path, bare_remote, "base-advance")
    _write(work, path, text)
    identity = ["-c", "user.email=human@example.invalid", "-c", "user.name=human"]
    _git(["add", "-A"], work)
    _git([*identity, "commit", "-q", "-m", "advance base"], work)
    _git(["push", "-q", "origin", "main"], work)
    return _capture(work, "rev-parse", "HEAD")


def _parents(cwd: str, commit: str) -> list[str]:
    return _capture(cwd, "rev-list", "--parents", "-n", "1", commit).split()[1:]


def _is_ancestor(cwd: str, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=cwd,
        capture_output=True,
    )
    return proc.returncode == 0


async def _publish_then_move_the_base(host: RealGitHost, bare_remote: str, tmp_path):
    earlier = _workspace(tmp_path, bare_remote, "earlier")
    _write(earlier, "src/app.py", "print('first run')\n")
    previous = await host.commit_and_push(_stage(earlier, "exec-1"))
    moved_base = _advance_base(tmp_path, bare_remote, "docs/notes.md", "landed on main\n")
    rerun = _workspace(tmp_path, bare_remote, "rerun")
    _write(rerun, "src/app.py", "print('second run')\n")
    return previous, moved_base, rerun


async def test_publish_after_the_base_moved_keeps_the_merge_base_diff_to_the_change(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    _, moved_base, rerun = await _publish_then_move_the_base(host, bare_remote, tmp_path)

    result = await host.commit_and_push(_stage(rerun, "exec-2"))

    assert _is_ancestor(rerun, moved_base, result.commit_sha)
    changed = _capture(rerun, "diff", "--name-only", f"{moved_base}...{result.commit_sha}")
    assert changed.split() == ["src/app.py"]


async def test_publish_after_the_base_moved_fast_forwards_the_branch(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    previous, moved_base, rerun = await _publish_then_move_the_base(host, bare_remote, tmp_path)
    request = _stage(rerun, "exec-2")

    result = await host.commit_and_push(request)

    assert result.pushed is True
    assert result.published_tree == request.tree
    assert _parents(rerun, result.commit_sha) == [previous.commit_sha, moved_base]
    assert _is_ancestor(rerun, previous.commit_sha, result.commit_sha)


async def test_repair_publish_within_an_execution_keeps_a_single_parent(
    bare_remote: str, tmp_path
) -> None:
    host = _host(bare_remote)
    _, _, rerun = await _publish_then_move_the_base(host, bare_remote, tmp_path)
    first = await host.commit_and_push(_stage(rerun, "exec-2"))

    _write(rerun, "src/app.py", "print('repaired')\n")
    repaired = await host.commit_and_push(_stage(rerun, "exec-2"))

    assert _parents(rerun, repaired.commit_sha) == [first.commit_sha]


async def test_rerun_on_an_unchanged_base_keeps_a_single_parent(bare_remote: str, tmp_path) -> None:
    host = _host(bare_remote)
    earlier = _workspace(tmp_path, bare_remote, "earlier")
    _write(earlier, "src/app.py", "print('first run')\n")
    previous = await host.commit_and_push(_stage(earlier, "exec-1"))
    rerun = _workspace(tmp_path, bare_remote, "rerun")
    _write(rerun, "src/app.py", "print('second run')\n")

    result = await host.commit_and_push(_stage(rerun, "exec-2"))

    assert _parents(rerun, result.commit_sha) == [previous.commit_sha]
