"""Exit proof: a bounded allowlisted task reaches a draft PR through every guard.

Hermetic. Composes Jira-canonical intake, an atomic work claim, deterministic workspace
materialization, a bounded code step under the caps, base-branch synchronization, the
pre-PR Jira recheck, and the GHES mediator opening a draft PR idempotently with a linkback,
every write passing the coordination idempotency ledger and sequence gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.real.git_host import RealGitHost
from scripts.config import Settings
from services.coordination import (
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
)
from services.coordination.orphans import InMemoryOrphanRegistry
from services.integration.github.credentials import build_single_identity_resolver
from services.integration.github.mediator import GitHubMediator
from services.integration.jira import (
    APPLIED,
    DATACENTER,
    SKIPPED,
    SnapshotCandidate,
    TaskSnapshot,
)
from services.integration.jira.linkback import post_pr_linkback
from services.integration.jira.snapshot import snapshot_id_for
from services.orchestration.bounded_task_pipeline import (
    DRAFT_PR_OPEN,
    EXISTING_PR_REUSED,
    NO_CHANGES,
    CodeResult,
    PipelineDeps,
    PipelineRequest,
    run_bounded_task,
)
from services.orchestration.branch_sync import GitBranchSynchronizer
from services.orchestration.materialization import MaterializedWorkspace, WorkspaceMaterializer

REPO = "agentic-demo-service"
OWNER = "acme"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _remote_head(remote: str, branch: str = "main") -> str:
    proc = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


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


def _fake_ghes() -> tuple[httpx.MockTransport, dict[str, list[dict]]]:
    state: dict[str, list[dict]] = {"pulls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/pulls"):
            body = json.loads(request.content)
            number = len(state["pulls"]) + 1
            pull = {
                "number": number,
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
        if request.method == "GET" and path.endswith("/pulls"):
            head = request.url.params.get("head")
            matches = [
                p
                for p in state["pulls"]
                if p["state"] == "open" and (head is None or f"{OWNER}:{p['head']['ref']}" == head)
            ]
            return httpx.Response(200, json=matches)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler), state


def _dc_issue() -> dict:
    return {
        "key": TASK_ID,
        "fields": {
            "project": {"key": "AGENTDEV"},
            "summary": "Add retry to the client",
            "description": "Add retry with backoff.",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "labels": ["backend", "resilience"],
        },
    }


def _settings() -> Settings:
    return Settings(allowlist_jira_projects="AGENTDEV", jira_acceptance_field="")


class _InMemorySnapshotStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}

    async def persist(self, candidate: SnapshotCandidate) -> TaskSnapshot:
        history = self.by_execution.setdefault(candidate.execution_id, [])
        if history and history[-1].content_hash == candidate.content_hash:
            return history[-1]
        version = history[-1].version + 1 if history else 1
        snapshot = TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, version),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=version,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )
        history.append(snapshot)
        return snapshot


class _FakeReader:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.calls = 0

    def get_issue(self, issue_key: str) -> dict:
        self.calls += 1
        return dict(self.issue)


class _FakeLinkbackClient:
    def __init__(self, source: str) -> None:
        self.source = source
        self.comments: list[str] = []

    def list_comment_bodies(self, issue_key: str) -> tuple[str, ...]:
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append(body)


class _FakeCodeStep:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.tasks: list[str] = []

    async def run(self, *, workspace: MaterializedWorkspace, task: str) -> CodeResult:
        self.tasks.append(task)
        for rel, text in self.files.items():
            target = Path(workspace.path) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return CodeResult(cost=0.2, tokens=150, llm_calls=2, terminal_reason="completed")


def _host(bare_remote: str, transport: httpx.MockTransport) -> RealGitHost:
    return RealGitHost(
        build_single_identity_resolver(
            api_url="http://ghes.invalid/api/v3",
            owner=OWNER,
            git_base_url="",
            token="test-token",
            repos={REPO},
        ),
        remote_url_for=lambda repo: bare_remote,
        transport=transport,
    )


async def test_bounded_task_reaches_draft_pr_hermetically(tmp_path, bare_remote) -> None:
    settings = _settings()
    base_sha = _remote_head(bare_remote)
    transport, ghes_state = _fake_ghes()
    host = _host(bare_remote, transport)
    ledger = InMemorySideEffectLedger()
    gate = InMemorySequenceGate()
    mediator = GitHubMediator(
        backend=host, ledger=ledger, gate=gate, allowed_repos=frozenset({REPO})
    )
    materializer = WorkspaceMaterializer(
        registry=InMemoryOrphanRegistry(),
        allowed_repos=frozenset({REPO}),
        workspace_root=tmp_path / "ws",
        remote_url_for=lambda repo: bare_remote,
    )
    synchronizer = GitBranchSynchronizer(
        allowed_repos=frozenset({REPO}),
        remote_url_for=lambda repo: bare_remote,
    )
    reader = _FakeReader(_dc_issue())
    linkback_client = _FakeLinkbackClient(DATACENTER)
    code_step = _FakeCodeStep({"README.md": "hello\nagent retry change\n"})

    deps = PipelineDeps(
        snapshot_store=_InMemorySnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=materializer,
        code_step=code_step,
        synchronizer=synchronizer,
        mediator=mediator,
        jira_reader=reader,
        linkback_client=linkback_client,
        settings=settings,
    )
    request = PipelineRequest(
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        repo=REPO,
        base="main",
        base_sha=base_sha,
        task="Add retry to the client",
        issue_key=TASK_ID,
        raw_issue=_dc_issue(),
        jira_source=DATACENTER,
    )

    result = await run_bounded_task(deps, request)

    branch = agent_branch_name(TASK_ID)
    assert result.status == DRAFT_PR_OPEN
    assert result.snapshot_id
    assert result.branch == branch
    assert result.pr_number == 1
    assert result.pr_url.endswith("/pr/1")
    assert result.linkback == APPLIED
    assert result.sequence_used == 3
    assert code_step.tasks == ["Add retry to the client"]
    assert reader.calls == 1

    assert gate.receipts == []

    branch_head = _remote_head(bare_remote, branch)
    assert branch_head and branch_head != base_sha
    assert len(ghes_state["pulls"]) == 1
    assert len(linkback_client.comments) == 1

    replay = await mediator.open_draft_pr(
        REPO, TASK_ID, EXECUTION_ID, branch, "main", "ignored", "ignored", sequence=3
    )
    assert replay.number == 1
    assert len(ghes_state["pulls"]) == 1

    again = post_pr_linkback(
        linkback_client,
        TASK_ID,
        execution_id=EXECUTION_ID,
        pr_url=result.pr_url,
        pr_title="ignored",
        repo=REPO,
        branch=branch,
    )
    assert again.action == SKIPPED
    assert len(linkback_client.comments) == 1

    second_code_step = _FakeCodeStep({"README.md": "should not run\n"})
    second_deps = PipelineDeps(
        snapshot_store=_InMemorySnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=materializer,
        code_step=second_code_step,
        synchronizer=synchronizer,
        mediator=mediator,
        jira_reader=reader,
        linkback_client=linkback_client,
        settings=settings,
    )
    second_request = PipelineRequest(
        task_id=TASK_ID,
        execution_id="exec-2",
        repo=REPO,
        base="main",
        base_sha=base_sha,
        task="Add retry to the client",
        issue_key=TASK_ID,
        raw_issue=_dc_issue(),
        jira_source=DATACENTER,
    )

    reused = await run_bounded_task(second_deps, second_request)

    assert reused.status == EXISTING_PR_REUSED
    assert reused.pr_number == result.pr_number
    assert reused.pr_url == result.pr_url
    assert reused.branch == branch
    assert reused.linkback == SKIPPED
    assert second_code_step.tasks == []
    assert len(ghes_state["pulls"]) == 1
    assert len(linkback_client.comments) == 1


async def test_bounded_task_no_changes_terminates_cleanly(tmp_path, bare_remote) -> None:
    settings = _settings()
    base_sha = _remote_head(bare_remote)
    transport, ghes_state = _fake_ghes()
    host = _host(bare_remote, transport)
    ledger = InMemorySideEffectLedger()
    gate = InMemorySequenceGate()
    mediator = GitHubMediator(
        backend=host, ledger=ledger, gate=gate, allowed_repos=frozenset({REPO})
    )
    materializer = WorkspaceMaterializer(
        registry=InMemoryOrphanRegistry(),
        allowed_repos=frozenset({REPO}),
        workspace_root=tmp_path / "ws",
        remote_url_for=lambda repo: bare_remote,
    )
    synchronizer = GitBranchSynchronizer(
        allowed_repos=frozenset({REPO}),
        remote_url_for=lambda repo: bare_remote,
    )
    reader = _FakeReader(_dc_issue())
    linkback_client = _FakeLinkbackClient(DATACENTER)
    code_step = _FakeCodeStep({})

    deps = PipelineDeps(
        snapshot_store=_InMemorySnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=materializer,
        code_step=code_step,
        synchronizer=synchronizer,
        mediator=mediator,
        jira_reader=reader,
        linkback_client=linkback_client,
        settings=settings,
    )
    request = PipelineRequest(
        task_id=TASK_ID,
        execution_id=EXECUTION_ID,
        repo=REPO,
        base="main",
        base_sha=base_sha,
        task="Add retry to the client",
        issue_key=TASK_ID,
        raw_issue=_dc_issue(),
        jira_source=DATACENTER,
    )

    result = await run_bounded_task(deps, request)

    assert result.status == NO_CHANGES
    assert result.terminal_reason == "agent produced no file changes"
    assert result.branch == agent_branch_name(TASK_ID)
    assert result.pr_number == 0
    assert ghes_state["pulls"] == []
    assert linkback_client.comments == []


@pytest.mark.skipif(
    not os.getenv("BOUNDED_TASK_LIVE_SMOKE"),
    reason="live bounded-task smoke needs VPN, JIRA_EMAIL, JIRA_API_TOKEN, acli, and a GHES token",
)
async def test_bounded_task_live_smoke() -> None:
    from services.integration.jira.adapter import build_jira_adapter
    from services.integration.jira.snapshot import PostgresTaskSnapshotStore

    repo = os.environ["BOUNDED_TASK_LIVE_REPO"]
    issue_key = os.environ["BOUNDED_TASK_LIVE_ISSUE"]
    execution_id = os.environ.get("BOUNDED_TASK_LIVE_EXECUTION", f"live-{issue_key}")
    base = os.environ.get("BOUNDED_TASK_LIVE_BASE", "main")

    adapter = build_jira_adapter()
    raw_issue = adapter.get_issue(issue_key)

    host = RealGitHost.from_settings()
    base_sha = _remote_head(host._remote_url(repo), base)
    ledger = InMemorySideEffectLedger()
    gate = InMemorySequenceGate()
    mediator = GitHubMediator.from_settings(host, ledger, gate)
    materializer = WorkspaceMaterializer.from_settings(InMemoryOrphanRegistry())
    synchronizer = GitBranchSynchronizer.from_settings()
    code_step = _FakeCodeStep({"docs/phase5-smoke.md": f"smoke {execution_id}\n"})

    deps = PipelineDeps(
        snapshot_store=PostgresTaskSnapshotStore(),
        claim_store=InMemoryWorkClaimStore(),
        materializer=materializer,
        code_step=code_step,
        synchronizer=synchronizer,
        mediator=mediator,
        jira_reader=adapter,
        linkback_client=adapter,
    )
    request = PipelineRequest(
        task_id=issue_key,
        execution_id=execution_id,
        repo=repo,
        base=base,
        base_sha=base_sha,
        task=raw_issue["fields"]["summary"],
        issue_key=issue_key,
        raw_issue=raw_issue,
        jira_source=adapter.source,
    )

    result = await run_bounded_task(deps, request)
    assert result.status == DRAFT_PR_OPEN
    assert result.pr_number >= 1
    assert result.linkback == APPLIED
