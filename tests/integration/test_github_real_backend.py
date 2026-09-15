"""Real GHES backend git mechanics against a bare remote and PRs against a fake transport."""

from __future__ import annotations

import json
import os
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


async def test_commit_and_push_is_idempotent_via_the_execution_trailer(bare_remote: str) -> None:
    host = _host(bare_remote)
    request = CommitRequest(
        repo=REPO,
        branch=agent_branch_name(TASK_ID),
        base="main",
        message="apply change",
        files={"src/app.py": "print('hi')\n"},
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
    )

    first = await host.commit_and_push(request)
    assert first.pushed is True
    assert first.replayed is False

    retry = await host.commit_and_push(request)
    assert retry.replayed is True
    assert retry.pushed is False
    assert retry.commit_sha == first.commit_sha


async def test_a_commit_clones_only_the_ref_it_needs(bare_remote: str, monkeypatch) -> None:
    """A full clone of an established repository has overrun the activity timeout."""
    from adapters.real import git_host as git_host_module

    host = _host(bare_remote)
    clones: list[list[str]] = []
    original = git_host_module.RealGitHost._git

    async def recording(self, *args: str, cwd: str | None = None) -> str:
        if args and args[0] == "clone":
            clones.append(list(args))
        return await original(self, *args, cwd=cwd)

    monkeypatch.setattr(git_host_module.RealGitHost, "_git", recording)

    await host.commit_and_push(
        CommitRequest(
            repo=REPO,
            branch=agent_branch_name(TASK_ID),
            base="main",
            message="apply change",
            files={"src/app.py": "print('hi')\n"},
            task_id=TASK_ID,
            execution_id=EXECUTION_ID,
        )
    )

    assert len(clones) == 1
    assert "--single-branch" in clones[0]
    assert "--filter=blob:none" in clones[0]
    assert clones[0][clones[0].index("--branch") + 1] == "main"


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
async def test_live_smoke_against_an_allowlisted_demo_repo() -> None:
    host = RealGitHost.from_settings()
    name = agent_branch_name(TASK_ID)
    branch = await host.ensure_branch(REPO, name, "main")
    assert branch.name == name
    request = CommitRequest(
        repo=REPO,
        branch=name,
        base="main",
        message="agent-platform live smoke",
        files={"AGENT_SMOKE.md": "live smoke\n"},
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
    )
    commit = await host.commit_and_push(request)
    assert commit.commit_sha
    pull = await host.open_draft_pr(REPO, name, "main", "Live smoke", "live smoke")
    assert pull.number
