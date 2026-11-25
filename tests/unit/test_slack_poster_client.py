"""The worker's client of the Slack posting service sends an alert's text and blocks over TLS with
the projected token and the remaining deadline, and reports every refusal with its error code."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from scripts.slack_poster_tls import issue_leaf, new_ca
from services import server_tls
from services.integration.slack.poster_client import (
    PostingFailed,
    post_message,
    read_group_members,
)
from services.slack_poster import DEADLINE_HEADER, GROUPS_PATH, MESSAGES_PATH
from tests.support import hermetic_settings

BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "Gate paused"}}]


def _ca_pem(ca) -> str:
    return ca.public_bytes(serialization.Encoding.PEM).decode()


@dataclass
class _Service:
    port: int
    ca_file: Path
    requests: list[dict[str, Any]] = field(default_factory=list)
    status: int = 200
    reply: dict[str, Any] = field(default_factory=lambda: {"ts": "1700000000.000100"})

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


def _serve(tmp_path: Path, names: list[str], ca=None) -> Iterator[_Service]:
    ca_key, ca = ca or new_ca()
    certificate, key = issue_leaf(ca_key, ca, names)
    (tmp_path / "leaf.pem").write_text(certificate)
    (tmp_path / "leaf-key.pem").write_text(key)
    (tmp_path / "ca.pem").write_text(_ca_pem(ca))
    state = _Service(port=0, ca_file=tmp_path / "ca.pem")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self._answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def do_GET(self) -> None:
            self._answer(None)

        def _answer(self, body: Any) -> None:
            state.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                    "peer": self.client_address,
                }
            )
            encoded = json.dumps(state.reply).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_tls.wrap(
        server,
        enabled=True,
        certificate_file=str(tmp_path / "leaf.pem"),
        key_file=str(tmp_path / "leaf-key.pem"),
    )
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[_Service]:
    yield from _serve(
        tmp_path, ["slack-poster.agentic-dev-platform.svc.cluster.local", "127.0.0.1"]
    )


def _settings(service: _Service, token_file: Path, **overrides: str):
    return hermetic_settings(
        **{
            "slack_posting_url": service.url,
            "slack_posting_token_file": str(token_file),
            "slack_posting_ca_file": str(service.ca_file),
            **overrides,
        }
    )


def test_a_post_carries_the_text_blocks_token_and_a_deadline_a_second_short_over_tls(
    service, tmp_path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token\n")

    post_message(_settings(service, token_file), text="Gate paused", blocks=BLOCKS, timeout=3.0)

    [request] = service.requests
    assert request["path"] == MESSAGES_PATH
    assert request["body"] == {"text": "Gate paused", "blocks": BLOCKS}
    assert request["headers"]["Authorization"] == "Bearer projected-token"
    assert request["headers"][DEADLINE_HEADER] == "2"


def test_a_group_read_carries_the_token_and_returns_the_members_over_tls(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token\n")
    service.reply = {"members": ["U0ONE", "U0TWO"]}

    members = read_group_members(_settings(service, token_file), "S0123456789", timeout=10.0)

    assert members == ["U0ONE", "U0TWO"]
    [request] = service.requests
    assert (request["method"], request["path"]) == ("GET", f"{GROUPS_PATH}S0123456789")
    assert request["headers"]["Authorization"] == "Bearer projected-token"


def test_a_refused_group_read_carries_the_error_code(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    service.status, service.reply = 403, {"error": "group_not_named"}

    with pytest.raises(PostingFailed, match="group_not_named") as refused:
        read_group_members(_settings(service, token_file), "S0123456789", timeout=10.0)

    assert refused.value.error == "group_not_named"


def test_the_token_is_read_from_the_file_for_every_post(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    settings = _settings(service, token_file)

    token_file.write_text("first")
    post_message(settings, text="one", blocks=None, timeout=3.0)
    token_file.write_text("rotated")
    post_message(settings, text="two", blocks=None, timeout=3.0)

    assert [request["headers"]["Authorization"] for request in service.requests] == [
        "Bearer first",
        "Bearer rotated",
    ]


def test_a_missing_token_file_names_the_file_and_sends_nothing(service, tmp_path) -> None:
    token_file = tmp_path / "absent-token"

    with pytest.raises(PostingFailed, match=str(token_file)):
        post_message(_settings(service, token_file), text="Gate paused", blocks=None, timeout=3.0)

    assert service.requests == []


@pytest.mark.parametrize(
    ("status", "reply", "code"),
    [
        (502, {"error": "not_in_channel"}, "not_in_channel"),
        (429, {"error": "ratelimited", "retry_after": 30.0}, "ratelimited"),
        (403, {"error": "channel_not_held"}, "channel_not_held"),
    ],
)
def test_a_refused_post_names_the_error_code(service, tmp_path, status, reply, code) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    service.status, service.reply = status, reply

    with pytest.raises(PostingFailed, match=code):
        post_message(_settings(service, token_file), text="Gate paused", blocks=None, timeout=3.0)


def test_an_unreachable_service_fails_the_post(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    settings = hermetic_settings(
        slack_posting_url="https://127.0.0.1:1",
        slack_posting_token_file=str(token_file),
        slack_posting_ca_file=str(service.ca_file),
    )

    with pytest.raises(PostingFailed, match="unreachable"):
        post_message(settings, text="Gate paused", blocks=None, timeout=3.0)


@pytest.mark.parametrize("timeout", [2.9, 2.5, 1.0])
def test_a_post_with_too_little_deadline_left_sends_nothing(service, tmp_path, timeout) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")

    with pytest.raises(PostingFailed, match="too little for the Slack posting service"):
        post_message(_settings(service, token_file), text="Gate", blocks=None, timeout=timeout)

    assert service.requests == []


def test_a_posting_url_without_tls_sends_nothing(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    settings = hermetic_settings(
        slack_posting_url=f"http://127.0.0.1:{service.port}",
        slack_posting_token_file=str(token_file),
    )

    with pytest.raises(PostingFailed, match="SLACK_POSTING_URL"):
        post_message(settings, text="Gate paused", blocks=None, timeout=3.0)

    assert service.requests == []


def test_a_service_presenting_a_leaf_of_another_ca_fails_naming_the_verification(
    service, tmp_path
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    (tmp_path / "other-ca.pem").write_text(_ca_pem(new_ca()[1]))
    settings = _settings(service, token_file, slack_posting_ca_file=str(tmp_path / "other-ca.pem"))

    with pytest.raises(PostingFailed, match="unreachable .*certificate verification failed"):
        post_message(settings, text="Gate paused", blocks=None, timeout=3.0)

    assert service.requests == []


def test_a_ca_file_rewritten_in_place_is_trusted_on_the_next_post(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    mounted = tmp_path / "mounted-ca.pem"
    mounted.write_text(_ca_pem(new_ca()[1]))
    settings = _settings(service, token_file, slack_posting_ca_file=str(mounted))
    with pytest.raises(PostingFailed, match="certificate verification failed"):
        post_message(settings, text="Gate paused", blocks=None, timeout=3.0)

    mounted.write_text(service.ca_file.read_text())
    os.utime(mounted, ns=(mounted.stat().st_atime_ns, mounted.stat().st_mtime_ns + 1_000_000))
    post_message(settings, text="Gate paused", blocks=None, timeout=3.0)

    assert len(service.requests) == 1


def test_a_leaf_of_the_trusted_ca_for_another_host_fails(tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    for other in _serve(tmp_path, ["other.agentic-dev-platform.svc.cluster.local"]):
        with pytest.raises(PostingFailed, match="certificate verification failed"):
            post_message(_settings(other, token_file), text="Gate", blocks=None, timeout=3.0)

        assert other.requests == []


def test_a_post_without_a_ca_file_sends_nothing_and_names_the_setting(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")

    with pytest.raises(PostingFailed, match="SLACK_POSTING_CA_FILE is not set"):
        post_message(
            _settings(service, token_file, slack_posting_ca_file=""),
            text="Gate paused",
            blocks=None,
            timeout=3.0,
        )

    assert service.requests == []


def test_two_posts_reuse_one_connection(service, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    settings = _settings(service, token_file)

    post_message(settings, text="one", blocks=None, timeout=3.0)
    post_message(settings, text="two", blocks=None, timeout=3.0)

    first, second = service.requests
    assert first["peer"] == second["peer"]


def test_a_ca_file_holding_two_cas_verifies_a_leaf_of_either(tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    old, new = new_ca("old CA"), new_ca("new CA")
    bundle = tmp_path / "bundle.pem"
    bundle.write_text(_ca_pem(old[1]) + _ca_pem(new[1]))

    for index, ca in enumerate((old, new)):
        directory = tmp_path / str(index)
        directory.mkdir()
        for issued in _serve(directory, ["127.0.0.1"], ca=ca):
            settings = _settings(issued, token_file, slack_posting_ca_file=str(bundle))
            post_message(settings, text="Gate", blocks=None, timeout=3.0)

            assert len(issued.requests) == 1
