"""Hermetic tests for the inbound CI status webhook that wakes the parked CI wait."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.integrations import HYBRID, POLLING, WEBHOOK
from services.orchestration.webhook.ci_events import CIWebhookHandler
from services.orchestration.webhook.inbound import handle_inbound

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "webhooks"
_ALLOWED = frozenset({"agentic-demo-service"})


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


class _Resolver:
    def __init__(self, workflow_id: str | None = "wf-ADSER-15") -> None:
        self._workflow_id = workflow_id
        self.asked: list[str] = []

    async def resolve_task(self, task_id: str) -> str | None:
        self.asked.append(task_id)
        return self._workflow_id


class _Signaller:
    def __init__(self) -> None:
        self.signalled: list[str] = []

    async def signal_ci_result(self, workflow_id: str) -> None:
        self.signalled.append(workflow_id)


def _handler(resolver=None, signaller=None, *, github_secret="", jenkins_secret=""):
    return CIWebhookHandler(
        github_secret=github_secret,
        jenkins_secret=jenkins_secret,
        resolver=resolver or _Resolver(),
        signaller=signaller or _Signaller(),
    )


async def _post(handler, *, body, mode=WEBHOOK, headers=None, ledger=None):
    return await handle_inbound(
        body,
        headers or {},
        handler=handler,
        allowed=_ALLOWED,
        ledger=ledger or InMemorySideEffectLedger(),
        mode=mode,
    )


async def test_jenkins_completion_signals_the_parked_workflow():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller)
    code, payload = await _post(handler, body=_fixture("jenkins_build_completed.json"))
    assert code == 200
    assert payload["status"] == "signalled"
    assert payload["source"] == "jenkins"
    assert signaller.signalled == ["wf-ADSER-15"]


async def test_github_actions_completion_signals_the_parked_workflow():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller)
    code, payload = await _post(
        handler, body=_fixture("github_actions_workflow_run_completed.json")
    )
    assert code == 200
    assert payload["source"] == "github"
    assert signaller.signalled == ["wf-ADSER-15"]


async def test_duplicate_delivery_is_a_noop():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller)
    ledger = InMemorySideEffectLedger()
    body = _fixture("jenkins_build_completed.json")
    first, _ = await _post(handler, body=body, ledger=ledger)
    second, second_payload = await _post(handler, body=body, ledger=ledger)
    assert first == 200
    assert second == 200
    assert second_payload["duplicate"] is True
    assert signaller.signalled == ["wf-ADSER-15"]


async def test_unverified_github_payload_is_refused():
    signaller = _Signaller()
    handler = _handler(_Resolver(), signaller, github_secret="s3cr3t")
    code, _ = await _post(
        handler,
        body=_fixture("github_actions_workflow_run_completed.json"),
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )
    assert code == 401
    assert signaller.signalled == []


async def test_valid_github_signature_is_accepted():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller, github_secret="s3cr3t")
    body = _fixture("github_actions_workflow_run_completed.json")
    signature = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    code, _ = await _post(handler, body=body, headers={"X-Hub-Signature-256": signature})
    assert code == 200
    assert signaller.signalled == ["wf-ADSER-15"]


async def test_no_parked_workflow_returns_404():
    signaller = _Signaller()
    handler = _handler(_Resolver(None), signaller)
    code, _ = await _post(handler, body=_fixture("jenkins_build_completed.json"))
    assert code == 404
    assert signaller.signalled == []


async def test_unlisted_repo_is_refused():
    body = json.loads(_fixture("jenkins_build_completed.json").decode())
    body["build"]["parameters"]["REPO"] = "ghost-repo"
    code, _ = await _post(_handler(), body=json.dumps(body).encode())
    assert code == 403


async def test_non_completion_event_is_ignored():
    body = json.loads(_fixture("jenkins_build_completed.json").decode())
    body["build"]["phase"] = "STARTED"
    code, payload = await _post(_handler(), body=json.dumps(body).encode())
    assert code == 202
    assert payload["status"] == "ignored"


async def test_polling_mode_disables_the_ci_webhook():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller)
    code, _ = await _post(handler, body=_fixture("jenkins_build_completed.json"), mode=POLLING)
    assert code == 202
    assert signaller.signalled == []


async def test_hybrid_mode_accepts_the_ci_webhook():
    signaller = _Signaller()
    handler = _handler(_Resolver("wf-ADSER-15"), signaller)
    code, _ = await _post(handler, body=_fixture("jenkins_build_completed.json"), mode=HYBRID)
    assert code == 200
    assert signaller.signalled == ["wf-ADSER-15"]
