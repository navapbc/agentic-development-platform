"""Real Jenkins backend: builds triggered and polled over the Jenkins REST API."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from adapters.interfaces.ci_host import (
    COMPLETED,
    QUEUED,
    RUNNING,
    BuildLog,
    BuildRef,
    BuildResult,
    failure_summary,
)
from scripts.config import Settings, get_settings


class RealJenkins:
    def __init__(
        self,
        *,
        base_url: str,
        user: str,
        api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._api_token = api_token
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RealJenkins:
        resolved = settings or get_settings()
        return cls(
            base_url=resolved.jenkins_url,
            user=resolved.jenkins_user,
            api_token=resolved.jenkins_api_token,
            transport=transport,
        )

    def _client(self) -> httpx.AsyncClient:
        auth = httpx.BasicAuth(self._user, self._api_token) if self._user else None
        return httpx.AsyncClient(
            base_url=self._base_url,
            auth=auth,
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
        params: dict[str, str] = {
            "EXECUTION_ID": execution_id,
            "BRANCH": branch,
            "COMMIT_SHA": commit_sha,
        }
        if parameters:
            params.update(parameters)
        async with self._client() as client:
            resp = await client.post(f"/job/{job}/buildWithParameters", params=params)
            resp.raise_for_status()
            location = resp.headers.get("Location", "")
        return await self.resolve_queue(repo, job, _queue_id_from_location(location))

    async def resolve_queue(self, repo: str, job: str, queue_id: int) -> BuildRef:
        if queue_id <= 0:
            return BuildRef(
                repo=repo, job=job, queue_id=queue_id, build_number=0, url="", triggered=True
            )
        async with self._client() as client:
            resp = await client.get(f"/queue/item/{queue_id}/api/json")
            if resp.status_code == httpx.codes.NOT_FOUND:
                return BuildRef(
                    repo=repo, job=job, queue_id=queue_id, build_number=0, url="", triggered=True
                )
            resp.raise_for_status()
            data = resp.json()
        executable = data.get("executable") or {}
        return BuildRef(
            repo=repo,
            job=job,
            queue_id=queue_id,
            build_number=int(executable.get("number") or 0),
            url=str(executable.get("url") or ""),
            triggered=True,
        )

    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult:
        async with self._client() as client:
            resp = await client.get(f"/job/{job}/{build_number}/api/json")
            resp.raise_for_status()
            data = resp.json()
        building = bool(data.get("building", False))
        result = str(data.get("result") or "")
        if building:
            status = RUNNING
        elif result:
            status = COMPLETED
        else:
            status = QUEUED
        return BuildResult(
            repo=repo,
            job=job,
            build_number=build_number,
            status=status,
            result=result,
            url=str(data.get("url") or ""),
            completed=not building and bool(result),
        )

    async def get_build_log(self, repo: str, job: str, build_number: int) -> BuildLog:
        async with self._client() as client:
            resp = await client.get(f"/job/{job}/{build_number}/consoleText")
            resp.raise_for_status()
            text = resp.text
        return BuildLog(
            repo=repo,
            job=job,
            build_number=build_number,
            text=text,
            failure_summary=failure_summary(text),
        )


def _queue_id_from_location(location: str) -> int:
    tail = location.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0
