"""Hermetic tests for the inbound Jira webhook feeding the shared intake path."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from scripts.config import Settings
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.integrations import HYBRID, POLLING, WEBHOOK
from services.integration.jira import DATACENTER
from services.integration.jira.snapshot import TaskSnapshot, snapshot_id_for
from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeStart,
    reconcile_intake,
)
from services.orchestration.webhook.inbound import handle_inbound
from services.orchestration.webhook.jira_events import JiraIntakeHandler

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
_ALLOWED_PROJECTS = frozenset({"ADSER"})
_ALLOWED_REPOS = frozenset({"agentic-demo-service"})


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


@dataclass(frozen=True)
class _Cred:
    default_repo: str
    auth_mode: str = DATACENTER


class _Resolver:
    def __init__(self, default_repo: str = "agentic-demo-service") -> None:
        self._cred = _Cred(default_repo=default_repo)

    def resolve(self, project: str) -> _Cred | None:
        return self._cred if project in _ALLOWED_PROJECTS else None


class _Starter:
    def __init__(self) -> None:
        self.starts: list[IntakeStart] = []

    async def start(self, start: IntakeStart) -> str:
        self.starts.append(start)
        return f"wf-{start.issue_key}-{start.repo}"


class _Alerter:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str]] = []

    async def unroutable(self, *, issue_key: str, project: str, reason: str) -> None:
        self.alerts.append((issue_key, project, reason))


class _Seeder:
    def __init__(self) -> None:
        self.seeded: list[tuple[str, str, str]] = []

    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        self.seeded.append((execution_id, task_id, repo))


class _RecordingStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}

    async def persist(self, candidate) -> TaskSnapshot:
        history = self.by_execution.setdefault(candidate.execution_id, [])
        version = history[-1].version + 1 if history else 1
        snapshot = TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, version),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=version,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )
        history.append(snapshot)
        return snapshot


@dataclass
class _World:
    ledger: InMemorySideEffectLedger
    starter: _Starter
    alerter: _Alerter
    seeder: _Seeder
    store: _RecordingStore
    resolver: Any


def _world(default_repo: str = "agentic-demo-service") -> _World:
    return _World(
        ledger=InMemorySideEffectLedger(),
        starter=_Starter(),
        alerter=_Alerter(),
        seeder=_Seeder(),
        store=_RecordingStore(),
        resolver=_Resolver(default_repo),
    )


def _context(world: _World) -> IntakeContext:
    return IntakeContext(
        starter=world.starter,
        alerter=world.alerter,
        ledger=world.ledger,
        execution_seeder=world.seeder,
        snapshot_store=world.store,
        github_allow=_ALLOWED_REPOS,
        field_id="",
        settings=Settings(_env_file=None),
    )


def _handler(world: _World, *, secret: str = "") -> JiraIntakeHandler:
    return JiraIntakeHandler(
        secret=secret, jira_resolver=world.resolver, context=_context(world)
    )


async def _post(world: _World, *, body: bytes, mode: str, headers=None, secret: str = ""):
    return await handle_inbound(
        body,
        headers or {},
        handler=_handler(world, secret=secret),
        allowed=_ALLOWED_PROJECTS,
        ledger=world.ledger,
        mode=mode,
    )


async def _poll(world: _World, *, issue: dict):
    class _ReadSource:
        async def list_candidate_issues(self, project: str) -> list[dict]:
            return [dict(issue)] if project in _ALLOWED_PROJECTS else []

    return await reconcile_intake(
        read_source=_ReadSource(),
        starter=world.starter,
        alerter=world.alerter,
        ledger=world.ledger,
        execution_seeder=world.seeder,
        settings=Settings(_env_file=None),
        jira_resolver=world.resolver,
        snapshot_store=world.store,
    )


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.jira_allowed_projects",
        lambda settings=None: set(_ALLOWED_PROJECTS),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.allowed_repos",
        lambda settings=None: set(_ALLOWED_REPOS),
    )
    monkeypatch.setattr(
        "services.orchestration.intake_reconciler.acceptance_field",
        lambda settings=None: "",
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.allowed_projects",
        lambda settings=None: set(_ALLOWED_PROJECTS),
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.acceptance_field",
        lambda settings=None: "",
    )


async def test_posted_webhook_starts_one_execution():
    world = _world()
    code, payload = await _post(
        world, body=_fixture("jira_datacenter_issue_created.json"), mode=WEBHOOK
    )
    assert code == 200
    assert payload["started"] == 1
    assert [(s.issue_key, s.repo) for s in world.starter.starts] == [
        ("ADSER-42", "agentic-demo-service")
    ]


async def test_duplicate_delivery_is_a_noop():
    world = _world()
    body = _fixture("jira_datacenter_issue_created.json")
    first_code, _ = await _post(world, body=body, mode=WEBHOOK)
    second_code, second = await _post(world, body=body, mode=WEBHOOK)
    assert first_code == 200
    assert second_code == 200
    assert second["duplicate"] is True
    assert len(world.starter.starts) == 1


async def test_unverified_payload_is_refused():
    world = _world()
    code, payload = await _post(
        world,
        body=_fixture("jira_datacenter_issue_created.json"),
        mode=WEBHOOK,
        headers={"X-Hub-Signature": "sha256=deadbeef"},
        secret="s3cr3t",
    )
    assert code == 401
    assert world.starter.starts == []


async def test_valid_signature_is_accepted():
    world = _world()
    body = _fixture("jira_datacenter_issue_created.json")
    signature = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    code, payload = await _post(
        world, body=body, mode=WEBHOOK, headers={"X-Hub-Signature": signature}, secret="s3cr3t"
    )
    assert code == 200
    assert payload["started"] == 1


async def test_polling_mode_disables_webhook_and_poller_still_starts():
    world = _world()
    body = _fixture("jira_datacenter_issue_created.json")
    code, payload = await _post(world, body=body, mode=POLLING)
    assert code == 202
    assert world.starter.starts == []

    issue = json.loads(body)["issue"]
    await _poll(world, issue=issue)
    assert [(s.issue_key, s.repo) for s in world.starter.starts] == [
        ("ADSER-42", "agentic-demo-service")
    ]


async def test_webhook_then_poll_starts_one_execution():
    world = _world()
    body = _fixture("jira_datacenter_issue_created.json")
    await _post(world, body=body, mode=HYBRID)
    await _poll(world, issue=json.loads(body)["issue"])
    assert len(world.starter.starts) == 1


async def test_poll_then_webhook_starts_one_execution():
    world = _world()
    body = _fixture("jira_datacenter_issue_created.json")
    await _poll(world, issue=json.loads(body)["issue"])
    code, payload = await _post(world, body=body, mode=HYBRID)
    assert code == 200
    assert payload["started"] == 0
    assert len(world.starter.starts) == 1


async def test_cloud_adf_webhook_starts_and_normalizes_description():
    world = _world()
    code, payload = await _post(
        world, body=_fixture("jira_cloud_issue_created.json"), mode=WEBHOOK
    )
    assert code == 200
    assert payload["started"] == 1
    assert [(s.issue_key, s.repo) for s in world.starter.starts] == [
        ("ADSER-50", "agentic-demo-service")
    ]
    snapshot = world.store.by_execution[world.starter.starts[0].execution_id][0]
    expected = "Add a GET /health endpoint that returns 200 with a body of ok."
    assert snapshot.payload["description"] == expected

