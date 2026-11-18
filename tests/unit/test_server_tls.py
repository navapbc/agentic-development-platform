"""The platform HTTP services serve TLS with a certificate each task issues itself."""

from __future__ import annotations

import socket
import ssl
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from services import healthcheck, server_tls


class _Handler(BaseHTTPRequestHandler):
    timeout = 5

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def tls_server(tmp_path, monkeypatch) -> Iterator[int]:
    monkeypatch.setenv("SERVER_TLS_DIR", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server_tls.wrap(server, enabled=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _get(port: int, context: ssl.SSLContext) -> int:
    return httpx.get(f"https://127.0.0.1:{port}/health", timeout=5, verify=context).status_code


def test_a_client_trusting_the_issued_certificate_reaches_the_service(tls_server) -> None:
    assert _get(tls_server, server_tls.probe_context()) == 200


def test_the_health_probe_calls_the_service_over_tls(tls_server) -> None:
    assert healthcheck.main(["http", str(tls_server), "/health", "--tls"]) == 0
    assert healthcheck.main(["http", str(tls_server), "/health"]) == 1


def test_a_client_without_the_certificate_fails_verification(tls_server) -> None:
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _get(tls_server, ssl.create_default_context())


def test_the_server_refuses_tls_older_than_1_2(tmp_path) -> None:
    assert server_tls.server_context(tmp_path).minimum_version == ssl.TLSVersion.TLSv1_2


def test_a_failed_handshake_is_logged_without_a_traceback(tls_server, caplog, capsys) -> None:
    assert healthcheck.main(["http", str(tls_server), "/health"]) == 1

    for _ in range(50):
        if "refused a TLS connection" in caplog.text:
            break
        threading.Event().wait(0.05)
    assert "refused a TLS connection from 127.0.0.1" in caplog.text
    assert "Traceback" not in capsys.readouterr().err


def test_a_client_that_never_handshakes_does_not_block_others(tls_server) -> None:
    with socket.create_connection(("127.0.0.1", tls_server), timeout=5):
        assert _get(tls_server, server_tls.probe_context()) == 200


def test_the_key_is_readable_by_the_owner_only(tls_server, tmp_path) -> None:
    key = server_tls.certificate_dir() / server_tls.KEY

    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700


def test_a_disabled_server_stays_plain_http(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SERVER_TLS_DIR", str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server_tls.wrap(server, enabled=False)
    try:
        assert not isinstance(server.socket, ssl.SSLSocket)
        assert not (tmp_path / "server-tls").exists()
    finally:
        server.server_close()
