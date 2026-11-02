"""Request limits, endpoint authentication, and fail-closed verification on the HTTP services."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.client import HTTPConnection, HTTPResponse
from http.server import ThreadingHTTPServer

import pytest
from services.coordination import (
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
)
from services.coordination.auth import CoordinationAuthError
from services.coordination.idempotency import SideEffectRef
from services.coordination.service import build_handler
from services.coordination.service import main as coordination_main
from services.http_front_door import (
    MAX_REQUEST_BODY_BYTES,
    RequestRejected,
    bearer_token_accepted,
    declared_body_length,
)
from services.orchestration.coordination_client import CoordinationClient
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    claim_work_unit,
)
from services.orchestration.webhook.jira_events import verify_jira_signature
from services.orchestration.webhook.pr_events import verify_signature
from services.orchestration.webhook.server import require_webhook_secrets
from temporalio.exceptions import ApplicationError
from tests.support import hermetic_settings

SHARED_SECRET = "coordination-test-secret"
_RESERVE_BODY = b'{"task_id": "T-1", "execution_id": "e-1", "operation": "op", "target": "t"}'


def _names_the_remedy(message: str) -> bool:
    return all(part in message for part in ("COORDINATION_SHARED_SECRET", ".env", "inv local-up"))


@pytest.fixture
def coordination_address() -> Iterator[tuple[str, int]]:
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    handler = build_handler(
        InMemorySideEffectLedger(),
        InMemorySequenceGate(),
        InMemoryWorkClaimStore(),
        loop,
        shared_secret=SHARED_SECRET,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)


def _post(
    address: tuple[str, int],
    path: str,
    *,
    headers: dict[str, str],
    body: bytes = b"",
) -> HTTPResponse:
    host, port = address
    conn = HTTPConnection(host, port, timeout=10)
    conn.putrequest("POST", path, skip_accept_encoding=True)
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.endheaders()
    if body:
        conn.send(body)
    return conn.getresponse()


def _authorized(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {SHARED_SECRET}"}
    headers.update(extra or {})
    return headers


def test_over_cap_content_length_is_rejected_without_reading_the_body(coordination_address):
    response = _post(
        coordination_address,
        "/idempotency/reserve",
        headers=_authorized({"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)}),
    )
    assert response.status == 413


def test_non_numeric_content_length_is_rejected(coordination_address):
    response = _post(
        coordination_address,
        "/idempotency/reserve",
        headers=_authorized({"Content-Length": "banana"}),
    )
    assert response.status == 400


def test_mutating_request_without_the_shared_secret_is_refused(coordination_address):
    response = _post(
        coordination_address,
        "/idempotency/reserve",
        headers={"Content-Length": str(len(_RESERVE_BODY))},
        body=_RESERVE_BODY,
    )
    assert response.status == 401


def test_mutating_request_with_a_wrong_shared_secret_is_refused(coordination_address):
    response = _post(
        coordination_address,
        "/idempotency/reserve",
        headers={
            "Authorization": "Bearer wrong-secret",
            "Content-Length": str(len(_RESERVE_BODY)),
        },
        body=_RESERVE_BODY,
    )
    assert response.status == 401


def test_mutating_request_with_the_shared_secret_is_served(coordination_address):
    response = _post(
        coordination_address,
        "/idempotency/reserve",
        headers=_authorized({"Content-Length": str(len(_RESERVE_BODY))}),
        body=_RESERVE_BODY,
    )
    assert response.status == 200


def test_health_check_stays_unauthenticated(coordination_address, monkeypatch):
    monkeypatch.setattr("services.coordination.service._agent_platform_healthy", lambda loop: True)
    host, port = coordination_address
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/health")
    assert conn.getresponse().status == 200


def test_declared_body_length_accepts_an_absent_header():
    assert declared_body_length({}) == 0


def test_declared_body_length_rejects_a_negative_length():
    with pytest.raises(RequestRejected) as excinfo:
        declared_body_length({"Content-Length": "-1"})
    assert excinfo.value.status == 400


def test_bearer_token_is_refused_when_no_token_is_expected():
    assert bearer_token_accepted({"Authorization": "Bearer anything"}, "") is False


def test_bearer_token_is_refused_without_an_authorization_header():
    assert bearer_token_accepted({}, SHARED_SECRET) is False


def test_bearer_token_is_refused_for_another_authorization_scheme():
    headers = {"Authorization": f"Basic {SHARED_SECRET}"}
    assert bearer_token_accepted(headers, SHARED_SECRET) is False


def test_bearer_token_is_accepted_for_the_expected_value():
    assert bearer_token_accepted(_authorized(), SHARED_SECRET) is True


def test_coordination_service_refuses_to_start_without_a_shared_secret(monkeypatch):
    monkeypatch.setattr(
        "services.coordination.service.get_settings",
        lambda: hermetic_settings(coordination_shared_secret=""),
    )
    with pytest.raises(SystemExit) as excinfo:
        coordination_main()
    assert _names_the_remedy(str(excinfo.value))


def test_coordination_client_refuses_an_empty_token():
    with pytest.raises(CoordinationAuthError) as excinfo:
        CoordinationClient("http://127.0.0.1:1", token="")
    assert _names_the_remedy(str(excinfo.value))


def test_coordination_client_names_the_remedy_for_a_refused_token(coordination_address):
    host, port = coordination_address
    client = CoordinationClient(f"http://{host}:{port}", token="another-secret")
    ref = SideEffectRef(task_id="T-1", execution_id="e-1", operation="op", target="t")
    with pytest.raises(CoordinationAuthError) as excinfo:
        asyncio.run(client.reserve_side_effect(ref))
    assert _names_the_remedy(str(excinfo.value))


def test_a_missing_shared_secret_fails_the_claim_activity_without_retry(monkeypatch):
    """Temporal retries a bare RuntimeError forever, which a config error never outgrows."""
    monkeypatch.setattr(
        "services.orchestration.coordination_client.get_settings",
        lambda: hermetic_settings(coordination_shared_secret=""),
    )
    request = ClaimRequest(
        task_id="T-1",
        execution_id="e-1",
        work_unit="unit",
        agent_id="agent",
        owner_workflow_id="wf-1",
    )

    with pytest.raises(ApplicationError) as excinfo:
        asyncio.run(claim_work_unit(request))

    assert excinfo.value.non_retryable is True
    assert _names_the_remedy(str(excinfo.value))


def test_github_verification_fails_on_an_empty_secret():
    assert verify_signature("", b"{}", "sha256=deadbeef") is False


def test_jira_verification_fails_on_an_empty_secret():
    assert verify_jira_signature("", b"{}", "sha256=deadbeef") is False


def test_webhook_mode_without_a_secret_refuses_to_start(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.webhook.server.load_integrations_config",
        lambda settings: _ModesConfig({"pr_closed": "webhook", "jira_intake": "hybrid"}),
    )
    with pytest.raises(SystemExit) as excinfo:
        require_webhook_secrets(hermetic_settings())
    assert "GITHUB_WEBHOOK_SECRET" in str(excinfo.value)
    assert "JIRA_WEBHOOK_SECRET" in str(excinfo.value)


def test_polling_mode_starts_without_a_secret(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.webhook.server.load_integrations_config",
        lambda settings: _ModesConfig({"pr_closed": "polling", "jira_intake": "polling"}),
    )
    require_webhook_secrets(hermetic_settings())


class _ModesConfig:
    def __init__(self, modes: dict[str, str]) -> None:
        self._modes = modes

    def webhook_enabled(self, name: str) -> bool:
        return self._modes.get(name, "polling") in ("webhook", "hybrid")
