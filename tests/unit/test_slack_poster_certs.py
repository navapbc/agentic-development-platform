"""Issuing the Slack posting service certificate from the platform CA and storing it for EKS."""

from __future__ import annotations

import json
import shutil
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from scripts import slack_poster_certs
from scripts import temporal_certs as certs
from services import server_tls

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

ARNS = {"slack_poster_tls": "arn:slack-poster-tls"}


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


class _Secrets:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:  # noqa: N803
        self.writes.append((SecretId, json.loads(SecretString)))


def _issued(tmp_path: Path, name: str = slack_poster_certs.SERVER_NAME) -> Path:
    directory = tmp_path / "tls"
    certs.generate(
        directory,
        frontend_names=["frontend.temporal.internal"],
        internode_names=["internode.temporal.internal"],
        frontend=False,
        repo_root=Path("/nonexistent-repository"),
    )
    certs.issue_server_certificate(
        directory,
        slack_poster_certs.CERTIFICATE,
        slack_poster_certs.KEY,
        [name],
        ip_addresses=(slack_poster_certs.LOOPBACK,),
        repo_root=Path("/nonexistent-repository"),
    )
    return directory


def test_the_stored_certificate_passes_a_default_client_verification(
    tmp_path,
) -> None:
    directory = _issued(tmp_path)
    secrets = _Secrets()

    slack_poster_certs.put(directory, ARNS, secrets)

    secret_id, value = secrets.writes[0]
    assert secret_id == "arn:slack-poster-tls"
    assert value["certificate"] == (directory / slack_poster_certs.CERTIFICATE).read_text()
    assert value["private_key"] == (directory / slack_poster_certs.KEY).read_text()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    server_tls.wrap(
        server,
        enabled=True,
        certificate_file=str(directory / slack_poster_certs.CERTIFICATE),
        key_file=str(directory / slack_poster_certs.KEY),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        context = ssl.create_default_context(cafile=directory / certs.CA)
        response = httpx.get(f"https://127.0.0.1:{server.server_address[1]}/", verify=context)
    finally:
        server.shutdown()
        server.server_close()
    assert response.status_code == 200


def test_a_certificate_for_another_name_is_not_stored(tmp_path) -> None:
    directory = _issued(tmp_path, name="slack-poster.other.svc.cluster.local")

    with pytest.raises(certs.CertificateError, match="does not name"):
        slack_poster_certs.put(directory, ARNS, _Secrets())


def test_put_without_the_secret_names_the_variable_to_set(tmp_path) -> None:
    with pytest.raises(certs.CertificateError, match="slack_shared_app"):
        slack_poster_certs.put(tmp_path, {}, _Secrets())
