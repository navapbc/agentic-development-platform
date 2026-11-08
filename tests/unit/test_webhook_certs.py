"""The self-signed inbound webhook certificate and its ACM import."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import webhook_certs
from scripts.temporal_certs import CertificateError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

NAME = "*.us-east-1.elb.amazonaws.com"


class _FakeAcm:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def import_certificate(self, **request):
        self.requests.append(request)
        return {
            "CertificateArn": request.get(
                "CertificateArn", "arn:aws:acm:us-east-1:1:certificate/new"
            )
        }


def test_the_default_name_covers_every_load_balancer_in_the_region() -> None:
    assert webhook_certs.default_name("us-gov-west-1") == "*.us-gov-west-1.elb.amazonaws.com"


def test_issue_writes_a_server_certificate_for_the_name(tmp_path) -> None:
    certificate, key = webhook_certs.issue(tmp_path / "tls", NAME)

    text = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"DNS:{NAME}" in text
    assert "TLS Web Server Authentication" in text
    assert "CA:FALSE" in text
    assert key.stat().st_mode & 0o077 == 0
    matched = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(certificate),
            "-noout",
            "-checkhost",
            "k8s-x-1.us-east-1.elb.amazonaws.com",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "does match certificate" in matched


def test_issue_refuses_the_repository_and_an_existing_certificate(tmp_path) -> None:
    with pytest.raises(CertificateError, match="inside the repository"):
        webhook_certs.issue(Path(__file__).parent / "tls", NAME)

    webhook_certs.issue(tmp_path, NAME)
    with pytest.raises(CertificateError, match="Pass --force"):
        webhook_certs.issue(tmp_path, NAME)
    webhook_certs.issue(tmp_path, NAME, force=True)


def test_import_reimports_into_the_tracked_arn(tmp_path) -> None:
    webhook_certs.issue(tmp_path, NAME)
    acm = _FakeAcm()

    first = webhook_certs.import_certificate(tmp_path, "", acm)
    again = webhook_certs.import_certificate(tmp_path, first, acm)

    assert first == "arn:aws:acm:us-east-1:1:certificate/new" == again
    assert "CertificateArn" not in acm.requests[0]
    assert acm.requests[1]["CertificateArn"] == first
    assert acm.requests[0]["Certificate"].startswith(b"-----BEGIN CERTIFICATE-----")


def test_import_refuses_a_directory_without_the_certificate(tmp_path) -> None:
    with pytest.raises(CertificateError, match="Run issue first"):
        webhook_certs.import_certificate(tmp_path, "", _FakeAcm())
