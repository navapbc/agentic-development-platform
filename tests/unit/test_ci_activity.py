"""Activity-level coverage for CI polling when the queue race leaves the build number unresolved."""

from __future__ import annotations

import pytest
from adapters.mock.ci_host import MockCIHost
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.jenkins.mediator import JenkinsMediator
from services.orchestration.temporal_activities import ci as ci_activity
from services.orchestration.temporal_activities.ci import PollCIRequest, poll_ci

REPO = "agentic-demo-service"
JOB = "agentic-demo-service-ci"
EXECUTION_ID = "exec-1"


def _mediator(backend: MockCIHost) -> JenkinsMediator:
    return JenkinsMediator(
        backend=backend,
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )


@pytest.fixture
def _patch_mediator(monkeypatch: pytest.MonkeyPatch) -> MockCIHost:
    backend = MockCIHost()
    backend.seed_job(
        REPO, JOB, result="FAILURE", log="tests\ntest_x FAILED\nAssertionError: boom\n"
    )
    mediator = _mediator(backend)
    monkeypatch.setattr(ci_activity, "_build_jenkins_mediator", lambda: mediator)
    return backend


async def test_poll_resolves_the_build_by_execution_id_when_the_number_is_zero(
    _patch_mediator: MockCIHost,
) -> None:
    await _patch_mediator.trigger_build(
        REPO, JOB, branch="agent/ADSER-1", commit_sha="abc", execution_id=EXECUTION_ID
    )
    result = await poll_ci(
        PollCIRequest(repo=REPO, job=JOB, build_number=0, execution_id=EXECUTION_ID)
    )
    assert result.build_number == 1
    assert result.completed is True
    assert result.result == "FAILURE"


async def test_poll_stays_queued_when_no_build_matches_the_execution(
    _patch_mediator: MockCIHost,
) -> None:
    result = await poll_ci(
        PollCIRequest(repo=REPO, job=JOB, build_number=0, execution_id="exec-absent")
    )
    assert result.build_number == 0
    assert result.status == "queued"
    assert result.completed is False
