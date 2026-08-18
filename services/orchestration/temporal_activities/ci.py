"""Temporal activities that trigger CI and read build results through the Jenkins mediator."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class TriggerCIRequest:
    repo: str
    task_id: str
    execution_id: str
    job: str
    branch: str
    commit_sha: str
    sequence: int


@dataclass
class TriggerCIResult:
    build_number: int
    queue_id: int
    url: str
    triggered: bool


@dataclass
class PollCIRequest:
    repo: str
    job: str
    build_number: int
    execution_id: str = ""


@dataclass
class PollCIResult:
    status: str
    result: str
    url: str
    completed: bool
    build_number: int = 0


@dataclass
class ReadCILogRequest:
    repo: str
    job: str
    build_number: int


@dataclass
class ReadCILogResult:
    failure_summary: str
    text: str


def _build_jenkins_mediator():
    from adapters.real.ci_host import RealJenkins

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.coordination.sequencing import PostgresSequenceGate
    from services.integration.jenkins.mediator import JenkinsMediator

    return JenkinsMediator.from_settings(
        RealJenkins.from_settings(),
        PostgresSideEffectLedger(),
        PostgresSequenceGate(),
    )


@activity.defn
async def trigger_ci(req: TriggerCIRequest) -> TriggerCIResult:
    mediator = _build_jenkins_mediator()
    ref = await mediator.trigger_build(
        req.repo,
        req.task_id,
        req.execution_id,
        req.job,
        branch=req.branch,
        commit_sha=req.commit_sha,
        sequence=req.sequence,
    )
    return TriggerCIResult(
        build_number=ref.build_number,
        queue_id=ref.queue_id,
        url=ref.url,
        triggered=ref.triggered,
    )


@activity.defn
async def poll_ci(req: PollCIRequest) -> PollCIResult:
    mediator = _build_jenkins_mediator()
    build_number = req.build_number
    if build_number <= 0 and req.execution_id:
        build_number = await mediator.find_build(req.repo, req.job, execution_id=req.execution_id)
    build = await mediator.get_build(req.repo, req.job, build_number)
    return PollCIResult(
        status=build.status,
        result=build.result,
        url=build.url,
        completed=build.completed,
        build_number=build_number,
    )


@activity.defn
async def read_ci_log(req: ReadCILogRequest) -> ReadCILogResult:
    mediator = _build_jenkins_mediator()
    log = await mediator.get_build_log(req.repo, req.job, req.build_number)
    return ReadCILogResult(failure_summary=log.failure_summary, text=log.text)
