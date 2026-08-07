"""Real GitHub Actions backend against a fake Actions REST transport."""

from __future__ import annotations

import os

import httpx
import pytest
from adapters.real.github_actions import RealGitHubActions, RepoNotAllowedError

OWNER = "agentic-org"
REPO = "agentic-demo-service"
JOB = "ci.yml"
BRANCH = "agent/AGENTDEV-1"
EXECUTION_ID = "exec-1"
COMMIT_SHA = "abc123"

_RUNS_PATH = f"/repos/{OWNER}/{REPO}/actions/workflows/{JOB}/runs"
_DISPATCH_PATH = f"/repos/{OWNER}/{REPO}/actions/workflows/{JOB}/dispatches"


def _run(run_id: int, *, status: str, conclusion: str) -> dict:
    return {
        "id": run_id,
        "head_sha": COMMIT_SHA,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.invalid/{OWNER}/{REPO}/actions/runs/{run_id}",
    }


def _fake_actions() -> tuple[httpx.MockTransport, dict]:
    state = {"dispatches": 0, "dispatched": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == _DISPATCH_PATH:
            state["dispatches"] += 1
            state["dispatched"] = True
            return httpx.Response(204)
        if request.method == "GET" and path == _RUNS_PATH:
            runs = (
                [_run(101, status="completed", conclusion="success")] if state["dispatched"] else []
            )
            return httpx.Response(200, json={"workflow_runs": runs})
        if request.method == "GET" and path == f"/repos/{OWNER}/{REPO}/actions/runs/101":
            return httpx.Response(200, json=_run(101, status="completed", conclusion="success"))
        if request.method == "GET" and path == f"/repos/{OWNER}/{REPO}/actions/runs/202":
            return httpx.Response(200, json=_run(202, status="completed", conclusion="failure"))
        if request.method == "GET" and path == f"/repos/{OWNER}/{REPO}/actions/runs/202/jobs":
            return httpx.Response(
                200,
                json={"jobs": [{"id": 2020, "name": "test", "conclusion": "failure"}]},
            )
        if request.method == "GET" and path == f"/repos/{OWNER}/{REPO}/actions/jobs/2020/logs":
            return httpx.Response(200, text="running tests\ntest_x FAILED\nAssertionError: boom\n")
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler), state


def _backend(transport: httpx.MockTransport) -> RealGitHubActions:
    return RealGitHubActions(
        api_url="https://github.invalid",
        owner=OWNER,
        token="token",
        allowed_repos=frozenset({REPO}),
        transport=transport,
    )


async def test_trigger_dispatches_once_and_replays_are_no_ops() -> None:
    transport, state = _fake_actions()
    backend = _backend(transport)

    first = await backend.trigger_build(
        REPO, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, execution_id=EXECUTION_ID
    )
    second = await backend.trigger_build(
        REPO, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, execution_id=EXECUTION_ID
    )

    assert first.triggered is True
    assert first.build_number == 101
    assert second.triggered is False
    assert second.build_number == 101
    assert state["dispatches"] == 1


async def test_get_build_reads_a_success_conclusion() -> None:
    transport, _ = _fake_actions()
    result = await _backend(transport).get_build(REPO, JOB, 101)

    assert result.result == "SUCCESS"
    assert result.status == "completed"
    assert result.completed is True


async def test_get_build_reads_a_failure_conclusion() -> None:
    transport, _ = _fake_actions()
    result = await _backend(transport).get_build(REPO, JOB, 202)

    assert result.result == "FAILURE"
    assert result.status == "completed"
    assert result.completed is True


async def test_get_build_log_reads_a_failure_summary() -> None:
    transport, _ = _fake_actions()
    log = await _backend(transport).get_build_log(REPO, JOB, 202)

    assert "AssertionError: boom" in log.failure_summary


async def test_trigger_refuses_a_non_allowlisted_repository() -> None:
    transport, state = _fake_actions()
    backend = _backend(transport)

    with pytest.raises(RepoNotAllowedError):
        await backend.trigger_build(
            "unlisted-service",
            JOB,
            branch=BRANCH,
            commit_sha=COMMIT_SHA,
            execution_id=EXECUTION_ID,
        )
    assert state["dispatches"] == 0


@pytest.mark.skipif(
    not os.getenv("ACTIONS_LIVE_SMOKE"),
    reason="requires a reachable GitHub Actions API and a token; set ACTIONS_LIVE_SMOKE to run",
)
async def test_live_smoke_dispatches_a_run_on_an_allowlisted_repository() -> None:
    backend = RealGitHubActions.from_settings()
    ref = await backend.trigger_build(
        REPO, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, execution_id=EXECUTION_ID
    )
    assert ref.triggered is True
