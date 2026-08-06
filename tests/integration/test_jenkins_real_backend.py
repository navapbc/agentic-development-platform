"""Real Jenkins backend against a fake Jenkins REST transport."""

from __future__ import annotations

import os

import httpx
import pytest
from adapters.real.ci_host import RealJenkins

REPO = "agentic-demo-service"
JOB = "agentic-demo-service-ci"
BRANCH = "agent/AGENTDEV-1"
EXECUTION_ID = "exec-1"
COMMIT_SHA = "abc123"


def _fake_jenkins() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == f"/job/{JOB}/buildWithParameters":
            return httpx.Response(
                201, headers={"Location": "http://jenkins.invalid/queue/item/42/"}
            )
        if request.method == "GET" and path == "/queue/item/42/api/json":
            return httpx.Response(
                200,
                json={"executable": {"number": 7, "url": f"http://jenkins.invalid/job/{JOB}/7/"}},
            )
        if request.method == "GET" and path == f"/job/{JOB}/7/api/json":
            return httpx.Response(
                200,
                json={
                    "building": False,
                    "result": "SUCCESS",
                    "url": f"http://jenkins.invalid/job/{JOB}/7/",
                },
            )
        if request.method == "GET" and path == f"/job/{JOB}/7/consoleText":
            return httpx.Response(200, text="running tests\nall passed\n")
        if request.method == "GET" and path == f"/job/{JOB}/8/api/json":
            return httpx.Response(
                200,
                json={
                    "building": False,
                    "result": "FAILURE",
                    "url": f"http://jenkins.invalid/job/{JOB}/8/",
                },
            )
        if request.method == "GET" and path == f"/job/{JOB}/8/consoleText":
            return httpx.Response(200, text="running tests\ntest_x FAILED\nAssertionError: boom\n")
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _backend() -> RealJenkins:
    return RealJenkins(
        base_url="http://jenkins.invalid",
        user="ci",
        api_token="token",
        transport=_fake_jenkins(),
    )


async def test_trigger_resolves_the_queue_item_to_a_build_number() -> None:
    ref = await _backend().trigger_build(
        REPO, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, execution_id=EXECUTION_ID
    )
    assert ref.triggered is True
    assert ref.queue_id == 42
    assert ref.build_number == 7


async def test_get_build_reads_a_success_result() -> None:
    result = await _backend().get_build(REPO, JOB, 7)
    assert result.result == "SUCCESS"
    assert result.status == "completed"
    assert result.completed is True


async def test_get_build_and_log_read_a_failure() -> None:
    backend = _backend()
    result = await backend.get_build(REPO, JOB, 8)
    assert result.result == "FAILURE"

    log = await backend.get_build_log(REPO, JOB, 8)
    assert "AssertionError: boom" in log.failure_summary


@pytest.mark.skipif(
    not os.getenv("JENKINS_LIVE_SMOKE"),
    reason="requires a reachable Jenkins and an API token; set JENKINS_LIVE_SMOKE to run",
)
async def test_live_smoke_triggers_a_build_on_local_jenkins() -> None:
    backend = RealJenkins.from_settings()
    ref = await backend.trigger_build(
        REPO, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, execution_id=EXECUTION_ID
    )
    assert ref.triggered is True
