"""Jenkins mediator over the in-memory backend and the in-process coordination surface."""

from __future__ import annotations

import pytest
from adapters.mock.ci_host import MockCIHost
from services.coordination.idempotency import InMemorySideEffectLedger
from services.coordination.sequencing import InMemorySequenceGate
from services.integration.jenkins.mediator import (
    JenkinsMediator,
    RepoNotAllowedError,
    StaleActionError,
)

REPO = "agentic-demo-service"
JOB = "agentic-demo-service-ci"
BRANCH = "agent/AGENTDEV-1"
TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"
COMMIT_SHA = "abc123"


@pytest.fixture
def backend() -> MockCIHost:
    host = MockCIHost()
    host.seed_job(REPO, JOB, result="SUCCESS", log="running tests\nall passed\n")
    return host


def _mediator(backend: MockCIHost) -> JenkinsMediator:
    return JenkinsMediator(
        backend=backend,
        ledger=InMemorySideEffectLedger(),
        gate=InMemorySequenceGate(),
        allowed_repos=frozenset({REPO}),
    )


@pytest.fixture
def mediator(backend: MockCIHost) -> JenkinsMediator:
    return _mediator(backend)


async def test_refuses_a_non_allowlisted_repository(mediator: JenkinsMediator) -> None:
    with pytest.raises(RepoNotAllowedError):
        await mediator.trigger_build(
            "secret-internal",
            TASK_ID,
            EXECUTION_ID,
            JOB,
            branch=BRANCH,
            commit_sha=COMMIT_SHA,
            sequence=1,
        )


async def test_trigger_is_idempotent_under_a_forced_retry(mediator: JenkinsMediator) -> None:
    first = await mediator.trigger_build(
        REPO, TASK_ID, EXECUTION_ID, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=1
    )
    assert first.triggered is True
    assert first.build_number == 1

    retry = await mediator.trigger_build(
        REPO, TASK_ID, EXECUTION_ID, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=1
    )
    assert retry == first


async def test_reads_a_success_result(mediator: JenkinsMediator) -> None:
    ref = await mediator.trigger_build(
        REPO, TASK_ID, EXECUTION_ID, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=1
    )
    result = await mediator.get_build(REPO, JOB, ref.build_number)
    assert result.status == "completed"
    assert result.result == "SUCCESS"
    assert result.completed is True


async def test_reads_a_failure_result_and_log_summary() -> None:
    backend = MockCIHost()
    backend.seed_job(
        REPO, JOB, result="FAILURE", log="running tests\ntest_x FAILED\nAssertionError: boom\n"
    )
    mediator = _mediator(backend)

    ref = await mediator.trigger_build(
        REPO, TASK_ID, EXECUTION_ID, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=1
    )
    result = await mediator.get_build(REPO, JOB, ref.build_number)
    assert result.result == "FAILURE"

    log = await mediator.get_build_log(REPO, JOB, ref.build_number)
    assert "AssertionError: boom" in log.failure_summary
    assert "FAILED" in log.failure_summary


async def test_a_stale_execution_trigger_is_discarded_by_the_sequence_gate(
    mediator: JenkinsMediator,
) -> None:
    await mediator.trigger_build(
        REPO, TASK_ID, "exec-new", JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=5
    )
    with pytest.raises(StaleActionError):
        await mediator.trigger_build(
            REPO, TASK_ID, "exec-old", JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=3
        )


async def test_find_build_resolves_a_build_by_execution_id(mediator: JenkinsMediator) -> None:
    ref = await mediator.trigger_build(
        REPO, TASK_ID, EXECUTION_ID, JOB, branch=BRANCH, commit_sha=COMMIT_SHA, sequence=1
    )
    assert await mediator.find_build(REPO, JOB, execution_id=EXECUTION_ID) == ref.build_number
    assert await mediator.find_build(REPO, JOB, execution_id="exec-absent") == 0
