"""In-memory CI host backend for hermetic tests and local runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from adapters.interfaces.ci_host import (
    COMPLETED,
    RUNNING,
    SUCCESS,
    BuildLog,
    BuildRef,
    BuildResult,
    failure_summary,
)


def _build_url(repo: str, job: str, build_number: int) -> str:
    return f"https://jenkins.invalid/{repo}/job/{job}/{build_number}/"


@dataclass
class _Build:
    build_number: int
    queue_id: int
    execution_id: str
    branch: str
    commit_sha: str
    result: str
    log: str
    building: bool = False


@dataclass
class _Job:
    builds: list[_Build] = field(default_factory=list)
    next_result: str = SUCCESS
    next_log: str = ""


class MockCIHost:
    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], _Job] = {}

    def seed_job(self, repo: str, job: str, *, result: str = SUCCESS, log: str = "") -> None:
        self._jobs[(repo, job)] = _Job(next_result=result, next_log=log)

    def _job(self, repo: str, job: str) -> _Job:
        return self._jobs.setdefault((repo, job), _Job())

    def _find_build(self, repo: str, job: str, build_number: int) -> _Build:
        record = self._job(repo, job)
        build = next((b for b in record.builds if b.build_number == build_number), None)
        if build is None:
            raise KeyError(f"build missing: {repo}:{job}#{build_number}")
        return build

    async def trigger_build(
        self,
        repo: str,
        job: str,
        *,
        branch: str,
        commit_sha: str,
        execution_id: str,
        parameters: Mapping[str, str] | None = None,
    ) -> BuildRef:
        record = self._job(repo, job)
        for existing in record.builds:
            if existing.execution_id == execution_id:
                return BuildRef(
                    repo=repo,
                    job=job,
                    queue_id=existing.queue_id,
                    build_number=existing.build_number,
                    url=_build_url(repo, job, existing.build_number),
                    triggered=False,
                )
        number = len(record.builds) + 1
        record.builds.append(
            _Build(
                build_number=number,
                queue_id=number,
                execution_id=execution_id,
                branch=branch,
                commit_sha=commit_sha,
                result=record.next_result,
                log=record.next_log,
            )
        )
        return BuildRef(
            repo=repo,
            job=job,
            queue_id=number,
            build_number=number,
            url=_build_url(repo, job, number),
            triggered=True,
        )

    async def resolve_queue(self, repo: str, job: str, queue_id: int) -> BuildRef:
        record = self._job(repo, job)
        build = next((b for b in record.builds if b.queue_id == queue_id), None)
        if build is None:
            return BuildRef(
                repo=repo, job=job, queue_id=queue_id, build_number=0, url="", triggered=True
            )
        return BuildRef(
            repo=repo,
            job=job,
            queue_id=queue_id,
            build_number=build.build_number,
            url=_build_url(repo, job, build.build_number),
            triggered=True,
        )

    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult:
        build = self._find_build(repo, job, build_number)
        return BuildResult(
            repo=repo,
            job=job,
            build_number=build_number,
            status=RUNNING if build.building else COMPLETED,
            result="" if build.building else build.result,
            url=_build_url(repo, job, build_number),
            completed=not build.building,
        )

    async def get_build_log(self, repo: str, job: str, build_number: int) -> BuildLog:
        build = self._find_build(repo, job, build_number)
        return BuildLog(
            repo=repo,
            job=job,
            build_number=build_number,
            text=build.log,
            failure_summary=failure_summary(build.log),
        )
