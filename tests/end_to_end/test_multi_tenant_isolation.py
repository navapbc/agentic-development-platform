"""End-to-end proof that two repos and two Jira projects run on one platform, no shared token.

Hermetic. Two allowlisted repositories owned by different accounts with different tokens each
reach a draft PR, two allowlisted Jira projects on different backends each receive their moves and
a linkback, and a no-review-status board keeps its ticket in the work-started status with the PR
linked, no review transition, and no move to Done. The live no-mocks path is the env-gated smoke
below and `inv demo-draft-pr` against two configured sources.
"""

from __future__ import annotations

import json
import os
import subprocess

import httpx
import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.real.git_host import RealGitHost
from services.coordination import (
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
)
from services.coordination.orphans import InMemoryOrphanRegistry
from services.integration.credentials import EnvSecretResolver
from services.integration.github.credentials import GitHubCredentialResolver, parse_github_config
from services.integration.github.mediator import GitHubMediator
from services.integration.jira import DATACENTER
from services.integration.jira.adapter import build_jira_adapter_for_issue
from services.integration.jira.credentials import JiraCredentialResolver, parse_jira_config
from services.orchestration.bounded_task_pipeline import (
    DRAFT_PR_OPEN,
    PipelineDeps,
    PipelineRequest,
    run_bounded_task,
)
from services.orchestration.branch_sync import GitBranchSynchronizer
from services.orchestration.materialization import WorkspaceMaterializer
from tests.integration.test_bounded_task_end_to_end import (
    _FakeCodeStep,
    _FakeLinkbackClient,
    _FakeReader,
    _InMemorySnapshotStore,
    _remote_head,
)
from tests.support import hermetic_settings

_GH_RAW = {
    "hosts": {
        "gh": {"api_url": "https://ghes.invalid/api/v3", "git_base_url": "https://ghes.invalid"}
    },
    "default": {"host": "gh", "owner": "org-a", "token_ref": "TOK_A"},
    "repositories": {"repo-b": {"owner": "org-b", "token_ref": "TOK_B"}},
}

_JIRA_RAW = {
    "sites": {
        "cloud": {"url": "https://acme.atlassian.net", "auth_mode": "cloud"},
        "dc": {"url": "https://jira.acme.internal", "auth_mode": "datacenter"},
    },
    "default": {
        "site": "cloud",
        "email": "agent@acme.com",
        "token_ref": "JTOK_C",
        "board": "CLOUDP",
        "statuses": ["To Do", "In Progress", "In Review", "Done"],
        "flow": {"work_started": "In Progress", "ready_for_review": "In Review"},
    },
    "projects": {
        "DCP": {
            "site": "dc",
            "token_ref": "JTOK_D",
            "board": "DCP",
            "statuses": ["Backlog", "In Progress", "Done"],
            "flow": {"work_started": "In Progress"},
        }
    },
}


def _bare_remote(tmp_path, name: str) -> str:
    remote = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True, capture_output=True
    )
    work = tmp_path / f"{name}-seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    ident = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    for args in (
        ["add", "."],
        [*ident, "commit", "-q", "-m", "init"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", *args], cwd=str(work), check=True, capture_output=True)
    return str(remote)


def _ghes_transport(calls: list[tuple[str, str | None]]) -> httpx.MockTransport:
    state: dict[str, list[dict]] = {"pulls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("authorization")))
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
                "draft": True,
                "state": "open",
            }
            state["pulls"].append(pull)
            return httpx.Response(201, json=pull)
        if request.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _issue(key: str, project: str) -> dict:
    return {
        "key": key,
        "fields": {
            "project": {"key": project},
            "summary": "Add retry to the client",
            "description": "Add retry with backoff.",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "labels": [],
        },
    }


@pytest.fixture(autouse=True)
def _allowlist_from_config(monkeypatch):
    monkeypatch.setattr(
        "services.integration.jira.snapshot.allowed_projects",
        lambda settings=None: {"CLOUDP", "DCP"},
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.acceptance_field",
        lambda settings=None: "",
    )


async def test_two_tenants_reach_a_draft_pr_with_no_shared_credential(tmp_path) -> None:
    remotes = {
        "repo-a": _bare_remote(tmp_path, "repo-a"),
        "repo-b": _bare_remote(tmp_path, "repo-b"),
    }
    calls: list[tuple[str, str | None]] = []
    host = RealGitHost(
        GitHubCredentialResolver(
            parse_github_config(_GH_RAW),
            {"repo-a", "repo-b"},
            EnvSecretResolver({"TOK_A": "tok-a", "TOK_B": "tok-b"}),
        ),
        transport=_ghes_transport(calls),
        remote_url_for=lambda repo: remotes[repo],
    )
    mediator = GitHubMediator(
        backend=host,
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({"repo-a", "repo-b"}),
    )
    jira = JiraCredentialResolver(
        parse_jira_config(_JIRA_RAW),
        {"CLOUDP", "DCP"},
        EnvSecretResolver({"JTOK_C": "jtok-c", "JTOK_D": "jtok-d"}),
    )
    settings = hermetic_settings()

    async def _run(repo: str, issue_key: str, project: str):
        materializer = WorkspaceMaterializer(
            registry=InMemoryOrphanRegistry(),
            allowed_repos=frozenset({repo}),
            workspace_root=tmp_path / f"ws-{repo}",
            remote_url_for=lambda r: remotes[r],
        )
        synchronizer = GitBranchSynchronizer(
            allowed_repos=frozenset({repo}), remote_url_for=lambda r: remotes[r]
        )
        reader = _FakeReader(_issue(issue_key, project))
        linkback = _FakeLinkbackClient(DATACENTER)
        deps = PipelineDeps(
            snapshot_store=_InMemorySnapshotStore(),
            claim_store=InMemoryWorkClaimStore(),
            materializer=materializer,
            code_step=_FakeCodeStep({"README.md": "hello\ntenant change\n"}),
            synchronizer=synchronizer,
            mediator=mediator,
            jira_reader=reader,
            linkback_client=linkback,
            settings=settings,
            status_flow=jira.status_flow(project),
        )
        request = PipelineRequest(
            task_id=issue_key,
            execution_id=f"exec-{issue_key}",
            repo=repo,
            base="main",
            base_sha=_remote_head(remotes[repo]),
            task="Add retry to the client",
            issue_key=issue_key,
            raw_issue=_issue(issue_key, project),
            jira_source=DATACENTER,
        )
        result = await run_bounded_task(deps, request)
        return result, reader, linkback

    result_a, reader_a, linkback_a = await _run("repo-a", "CLOUDP-1", "CLOUDP")
    result_b, reader_b, linkback_b = await _run("repo-b", "DCP-1", "DCP")

    assert result_a.status == DRAFT_PR_OPEN
    assert result_b.status == DRAFT_PR_OPEN

    a_calls = [(url, auth) for url, auth in calls if "/repos/org-a/repo-a/" in url]
    b_calls = [(url, auth) for url, auth in calls if "/repos/org-b/repo-b/" in url]
    assert a_calls and all(auth == "Bearer tok-a" for _, auth in a_calls)
    assert b_calls and all(auth == "Bearer tok-b" for _, auth in b_calls)
    assert not any("tok-b" in (auth or "") for _, auth in a_calls)
    assert not any("tok-a" in (auth or "") for _, auth in b_calls)

    assert _remote_head(remotes["repo-a"], agent_branch_name("CLOUDP-1"))
    assert _remote_head(remotes["repo-b"], agent_branch_name("DCP-1"))

    assert reader_a.transitions == ["In Progress", "In Review"]
    assert reader_b.transitions == ["In Progress"]
    assert "Done" not in reader_a.transitions
    assert "Done" not in reader_b.transitions
    assert len(linkback_a.comments) == 1
    assert len(linkback_b.comments) == 1

    cloud = jira.resolve("CLOUDP")
    dc = jira.resolve("DCP")
    assert cloud is not None and dc is not None
    assert cloud.url != dc.url
    assert cloud.token != dc.token
    cloud_adapter = build_jira_adapter_for_issue("CLOUDP-1", settings, resolver=jira)
    dc_adapter = build_jira_adapter_for_issue("DCP-1", settings, resolver=jira)
    assert type(cloud_adapter.client).__name__ == "AcliClient"
    assert type(dc_adapter.client).__name__ == "JiraDataCenterClient"


@pytest.mark.skipif(
    not os.getenv("MULTI_TENANT_LIVE_SMOKE"),
    reason="set MULTI_TENANT_LIVE_SMOKE with two configured repos and two Jira projects to run",
)
def test_live_two_sources_resolve_distinct_credentials() -> None:
    from services.integration.github.credentials import build_github_credential_resolver
    from services.integration.jira.credentials import build_jira_credential_resolver

    gh = build_github_credential_resolver()
    repos = sorted(gh.repos())
    if len(repos) < 2:
        pytest.skip("configure at least two allowlisted repositories in github.yaml")
    cred_a = gh.resolve(repos[0])
    cred_b = gh.resolve(repos[1])
    assert cred_a is not None and cred_b is not None
    assert (cred_a.owner, cred_a.token) != (cred_b.owner, cred_b.token)

    jira = build_jira_credential_resolver()
    projects = sorted(jira.projects())
    if len(projects) < 2:
        pytest.skip("configure at least two allowlisted projects in jira.yaml")
    proj_a = jira.resolve(projects[0])
    proj_b = jira.resolve(projects[1])
    assert proj_a is not None and proj_b is not None
    assert (proj_a.url, proj_a.token) != (proj_b.url, proj_b.token)
