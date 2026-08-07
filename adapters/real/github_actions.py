"""Real GitHub Actions backend: runs dispatched and read over the Actions REST API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from adapters.interfaces.ci_host import (
    ABORTED,
    COMPLETED,
    FAILURE,
    QUEUED,
    RUNNING,
    SUCCESS,
    UNSTABLE,
    BuildLog,
    BuildRef,
    BuildResult,
    failure_summary,
)
from scripts.config import Settings, get_settings

_FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
_RESULT_BY_CONCLUSION = {
    "success": SUCCESS,
    "failure": FAILURE,
    "timed_out": FAILURE,
    "startup_failure": FAILURE,
    "cancelled": ABORTED,
    "neutral": UNSTABLE,
    "skipped": UNSTABLE,
    "stale": UNSTABLE,
    "action_required": UNSTABLE,
}


class RepoNotAllowedError(Exception):
    def __init__(self, repo: str) -> None:
        super().__init__(f"repository not allowlisted: {repo}")
        self.repo = repo


class RealGitHubActions:
    def __init__(
        self,
        *,
        api_url: str,
        owner: str,
        token: str,
        allowed_repos: frozenset[str],
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._owner = owner
        self._token = token
        self._allowed_repos = allowed_repos
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RealGitHubActions:
        resolved = settings or get_settings()
        return cls(
            api_url=resolved.github_api_url,
            owner=resolved.github_owner,
            token=resolved.github_token,
            allowed_repos=frozenset(resolved.allowed_repos()),
            transport=transport,
        )

    def _guard(self, repo: str) -> None:
        if repo not in self._allowed_repos:
            raise RepoNotAllowedError(repo)

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return httpx.AsyncClient(
            base_url=self._api_url,
            headers=headers,
            transport=self._transport,
            timeout=self._timeout,
        )

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
        self._guard(repo)
        existing = await self._find_run(repo, job, branch, commit_sha)
        if existing is not None:
            return _ref_from_run(repo, job, existing, triggered=False)
        inputs: dict[str, str] = {"execution_id": execution_id, "commit_sha": commit_sha}
        if parameters:
            inputs.update(parameters)
        async with self._client() as client:
            resp = await client.post(
                f"/repos/{self._owner}/{repo}/actions/workflows/{job}/dispatches",
                json={"ref": branch, "inputs": inputs},
            )
            resp.raise_for_status()
        run = await self._find_run(repo, job, branch, commit_sha)
        if run is None:
            return BuildRef(repo=repo, job=job, queue_id=0, build_number=0, url="", triggered=True)
        return _ref_from_run(repo, job, run, triggered=True)

    async def resolve_queue(self, repo: str, job: str, queue_id: int) -> BuildRef:
        self._guard(repo)
        if queue_id <= 0:
            return BuildRef(
                repo=repo, job=job, queue_id=queue_id, build_number=0, url="", triggered=True
            )
        run = await self._get_run(repo, queue_id)
        return _ref_from_run(repo, job, run, triggered=True)

    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult:
        self._guard(repo)
        run = await self._get_run(repo, build_number)
        status, result, completed = _map_run(run)
        return BuildResult(
            repo=repo,
            job=job,
            build_number=build_number,
            status=status,
            result=result,
            url=str(run.get("html_url") or ""),
            completed=completed,
        )

    async def get_build_log(self, repo: str, job: str, build_number: int) -> BuildLog:
        self._guard(repo)
        async with self._client() as client:
            jobs_resp = await client.get(
                f"/repos/{self._owner}/{repo}/actions/runs/{build_number}/jobs"
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs") or []
            target = _select_failed_job(jobs)
            text = ""
            if target is not None:
                log_resp = await client.get(
                    f"/repos/{self._owner}/{repo}/actions/jobs/{target}/logs",
                    follow_redirects=True,
                )
                log_resp.raise_for_status()
                text = log_resp.text
        return BuildLog(
            repo=repo,
            job=job,
            build_number=build_number,
            text=text,
            failure_summary=failure_summary(text),
        )

    async def _get_run(self, repo: str, run_id: int) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.get(f"/repos/{self._owner}/{repo}/actions/runs/{run_id}")
            resp.raise_for_status()
            return resp.json()

    async def _find_run(
        self, repo: str, job: str, branch: str, commit_sha: str
    ) -> dict[str, Any] | None:
        async with self._client() as client:
            resp = await client.get(
                f"/repos/{self._owner}/{repo}/actions/workflows/{job}/runs",
                params={"branch": branch, "per_page": "30"},
            )
            resp.raise_for_status()
            runs = resp.json().get("workflow_runs") or []
        for run in runs:
            if str(run.get("head_sha") or "") == commit_sha:
                return run
        return None


def _ref_from_run(repo: str, job: str, run: Mapping[str, Any], *, triggered: bool) -> BuildRef:
    run_id = int(run.get("id") or 0)
    return BuildRef(
        repo=repo,
        job=job,
        queue_id=run_id,
        build_number=run_id,
        url=str(run.get("html_url") or ""),
        triggered=triggered,
    )


def _map_run(run: Mapping[str, Any]) -> tuple[str, str, bool]:
    raw_status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    if raw_status == "completed":
        status = COMPLETED
    elif raw_status == "in_progress":
        status = RUNNING
    else:
        status = QUEUED
    result = _RESULT_BY_CONCLUSION.get(conclusion, "")
    completed = raw_status == "completed" and bool(conclusion)
    return status, result, completed


def _select_failed_job(jobs: list[dict[str, Any]]) -> int | None:
    for job in jobs:
        if str(job.get("conclusion") or "") in _FAILED_CONCLUSIONS:
            return int(job.get("id") or 0)
    if jobs:
        return int(jobs[0].get("id") or 0)
    return None
