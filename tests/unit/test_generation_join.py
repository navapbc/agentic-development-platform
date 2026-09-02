"""Hermetic tests for joining model generations to the execution session."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from observability import attributes as attrs
from observability.context import bind_execution
from observability.generation import (
    current_langfuse_headers,
    install_litellm_header_propagation,
    langfuse_headers_from_identifiers,
    reset_header_propagation_for_testing,
)


def test_headers_map_platform_identifiers():
    headers = langfuse_headers_from_identifiers(
        {
            attrs.EXECUTION_ID: "exec-1",
            attrs.STAGE: "code_review",
            attrs.PROMPT_VERSION: "code-review/v1",
            attrs.TASK_ID: "TASK-1",
        }
    )
    assert headers == {
        "langfuse_session_id": "exec-1",
        "langfuse_generation_name": "code_review",
        "langfuse_version": "code-review/v1",
    }


def test_headers_empty_without_execution():
    assert langfuse_headers_from_identifiers({attrs.STAGE: "plan"}) == {
        "langfuse_generation_name": "plan"
    }
    assert langfuse_headers_from_identifiers({}) == {}


def test_current_headers_read_from_baggage():
    with bind_execution(
        execution_id="exec-2", stage="plan_review", prompt_version="plan-review/v1"
    ):
        headers = current_langfuse_headers()
    assert headers["langfuse_session_id"] == "exec-2"
    assert headers["langfuse_generation_name"] == "plan_review"
    assert headers["langfuse_version"] == "plan-review/v1"
    assert current_langfuse_headers() == {}


class _CaptureHandler(BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_POST(self) -> None:
        type(self).captured = {key.lower(): value for key, value in self.headers.items()}
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def capture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _CaptureHandler.captured = {}
    yield server
    server.shutdown()


async def _post(url: str) -> None:
    transport = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(url, json={"ping": True})


async def test_seam_injects_session_header_for_proxy_host(capture_server) -> None:
    host, port = capture_server.server_address
    base_url = f"http://{host}:{port}"
    reset_header_propagation_for_testing()
    original = httpx.AsyncHTTPTransport
    try:
        assert install_litellm_header_propagation(base_url) is True
        with bind_execution(
            execution_id="exec-live", stage="code_review", prompt_version="code-review/v1"
        ):
            await _post(f"{base_url}/v1/chat/completions")
    finally:
        httpx.AsyncHTTPTransport = original
        reset_header_propagation_for_testing()

    captured = _CaptureHandler.captured
    assert captured.get("langfuse_session_id") == "exec-live"
    assert captured.get("langfuse_generation_name") == "code_review"
    assert captured.get("langfuse_version") == "code-review/v1"


async def test_seam_skips_unbound_and_other_hosts(capture_server) -> None:
    host, port = capture_server.server_address
    base_url = f"http://{host}:{port}"
    reset_header_propagation_for_testing()
    original = httpx.AsyncHTTPTransport
    try:
        install_litellm_header_propagation(base_url)
        await _post(f"{base_url}/v1/chat/completions")
    finally:
        httpx.AsyncHTTPTransport = original
        reset_header_propagation_for_testing()

    assert "langfuse_session_id" not in _CaptureHandler.captured


async def test_seam_logs_error_when_a_completion_call_lacks_a_session(
    capture_server, caplog
) -> None:
    host, port = capture_server.server_address
    base_url = f"http://{host}:{port}"
    reset_header_propagation_for_testing()
    original = httpx.AsyncHTTPTransport
    try:
        install_litellm_header_propagation(base_url)
        with caplog.at_level("ERROR", logger="observability"):
            await _post(f"{base_url}/v1/chat/completions")
    finally:
        httpx.AsyncHTTPTransport = original
        reset_header_propagation_for_testing()

    assert any("generation join failed" in record.message for record in caplog.records)


async def test_seam_does_not_log_for_non_completion_paths(capture_server, caplog) -> None:
    host, port = capture_server.server_address
    base_url = f"http://{host}:{port}"
    reset_header_propagation_for_testing()
    original = httpx.AsyncHTTPTransport
    try:
        install_litellm_header_propagation(base_url)
        with caplog.at_level("ERROR", logger="observability"):
            await _post(f"{base_url}/health/liveliness")
    finally:
        httpx.AsyncHTTPTransport = original
        reset_header_propagation_for_testing()

    assert not any("generation join failed" in record.message for record in caplog.records)


def test_install_is_idempotent_and_noop_without_url():
    reset_header_propagation_for_testing()
    original = httpx.AsyncHTTPTransport
    try:
        assert install_litellm_header_propagation("") is False
        assert install_litellm_header_propagation("http://localhost:4000") is True
        assert install_litellm_header_propagation("http://localhost:4000") is False
    finally:
        httpx.AsyncHTTPTransport = original
        reset_header_propagation_for_testing()


def test_coding_extra_body_carries_join_and_bedrock_attribution():
    from openhands.runtime.conversation import _coding_extra_body

    body = _coding_extra_body(
        {"execution_id": "exec-3", "stage": "implementing", "ticket": "TASK-3"}
    )
    assert body["requestMetadata"] == {
        "execution_id": "exec-3",
        "stage": "implementing",
        "ticket": "TASK-3",
    }
    join = body["metadata"]
    assert join["session_id"] == "exec-3"
    assert join["generation_name"] == "implementing"
    assert join["version"]
    assert join["trace_metadata"] == {"execution_id": "exec-3", "ticket": "TASK-3"}


def test_coding_extra_body_empty_without_metadata():
    from openhands.runtime.conversation import _coding_extra_body

    assert _coding_extra_body(None) == {}
    assert _coding_extra_body({}) == {}


def test_coding_extra_body_round_trips_json():
    from openhands.runtime.conversation import _coding_extra_body

    body = _coding_extra_body({"execution_id": "exec-4", "stage": "implementing"})
    assert json.loads(json.dumps(body)) == body
