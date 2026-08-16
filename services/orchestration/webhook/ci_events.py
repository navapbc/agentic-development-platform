"""Parse, authenticate, and route inbound CI completion webhooks to the parked CI wait."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.orchestration.webhook.inbound import DispatchNotReady
from services.orchestration.webhook.pr_events import verify_signature

_GH_SIGNATURE_HEADER = "X-Hub-Signature-256"
_JENKINS_SIGNATURE_HEADER = "X-Jenkins-Signature"
_BRANCH_PREFIX = "agent/"
_JENKINS_COMPLETE_PHASES = frozenset({"COMPLETED", "FINALIZED"})

SOURCE_GITHUB = "github"
SOURCE_JENKINS = "jenkins"


def _task_id_from_branch(branch: str) -> str:
    return branch[len(_BRANCH_PREFIX) :] if branch.startswith(_BRANCH_PREFIX) else ""


@dataclass(frozen=True)
class CIEvent:
    source: str
    repo: str
    task_id: str
    delivery_id: str


def _parse_github(payload: dict[str, Any]) -> CIEvent | None:
    if payload.get("action") != "completed":
        return None
    run = payload["workflow_run"]
    if str(run.get("status")) != "completed":
        return None
    repo = str((payload.get("repository") or {}).get("name") or "")
    task_id = _task_id_from_branch(str(run.get("head_branch") or ""))
    run_id = run.get("id")
    if not repo or not task_id or not run_id:
        return None
    return CIEvent(source=SOURCE_GITHUB, repo=repo, task_id=task_id, delivery_id=f"github:{run_id}")


def _parse_jenkins(payload: dict[str, Any]) -> CIEvent | None:
    build = payload["build"]
    if str(build.get("phase")) not in _JENKINS_COMPLETE_PHASES:
        return None
    params = build.get("parameters") or {}
    repo = str(params.get("REPO") or "")
    task_id = _task_id_from_branch(str(params.get("BRANCH") or ""))
    number = build.get("number")
    if not repo or not task_id or not number:
        return None
    return CIEvent(
        source=SOURCE_JENKINS, repo=repo, task_id=task_id, delivery_id=f"jenkins:{repo}:{number}"
    )


def parse_ci_event(payload: dict[str, Any]) -> CIEvent | None:
    if isinstance(payload.get("workflow_run"), dict):
        return _parse_github(payload)
    if isinstance(payload.get("build"), dict):
        return _parse_jenkins(payload)
    return None


class CIWorkflowResolver(Protocol):
    async def resolve_task(self, task_id: str) -> str | None: ...


class CISignaller(Protocol):
    async def signal_ci_result(self, workflow_id: str) -> None: ...


class CIWebhookHandler:
    name = "ci_status"

    def __init__(
        self,
        *,
        github_secret: str,
        jenkins_secret: str,
        resolver: CIWorkflowResolver,
        signaller: CISignaller,
    ) -> None:
        self._github_secret = github_secret
        self._jenkins_secret = jenkins_secret
        self._resolver = resolver
        self._signaller = signaller

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        if headers.get(_GH_SIGNATURE_HEADER):
            return verify_signature(self._github_secret, body, headers[_GH_SIGNATURE_HEADER])
        return verify_signature(
            self._jenkins_secret, body, headers.get(_JENKINS_SIGNATURE_HEADER, "")
        )

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        event = parse_ci_event(payload)
        return event.delivery_id if event is not None else ""

    def parse(self, payload: dict[str, Any]) -> CIEvent | None:
        return parse_ci_event(payload)

    def allowlisted(self, event: CIEvent, allowed: frozenset[str]) -> bool:
        return event.repo in allowed

    async def dispatch(self, event: CIEvent) -> dict[str, Any]:
        workflow_id = await self._resolver.resolve_task(event.task_id)
        if not workflow_id:
            raise DispatchNotReady(f"no parked workflow for {event.task_id}")
        await self._signaller.signal_ci_result(workflow_id)
        return {"status": "signalled", "workflow_id": workflow_id, "source": event.source}
