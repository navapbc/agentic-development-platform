"""Issuing the MCP gateway certificate from the platform CA and importing it into ACM."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import mcp_gateway_certs
from scripts import temporal_certs as certs
from scripts.temporal_certs import CertificateError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

DOMAIN = "mcp.sandbox.internal"
OUTSIDE = Path("/nonexistent-repository")
WEBHOOK_ARN = "arn:aws:acm:us-east-1:1:certificate/webhook"
GATEWAY_ARN = "arn:aws:acm:us-east-1:1:certificate/gateway"
HOSTS = {"alpha": f"mcp-alpha.{DOMAIN}", "beta": f"mcp-beta.{DOMAIN}"}


class _FakeAcm:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def import_certificate(self, **request):
        self.requests.append(request)
        return {"CertificateArn": request.get("CertificateArn", "arn:aws:acm:us-east-1:1:new")}


def _ca(tmp_path: Path) -> Path:
    out = tmp_path / "internal-tls"
    certs.generate(
        out,
        frontend_names=["frontend.temporal.internal"],
        internode_names=["internode.temporal.internal"],
        frontend=False,
        internal_names=["coordination.platform.internal"],
        repo_root=OUTSIDE,
    )
    return out


def _issued(tmp_path: Path, domain: str = DOMAIN) -> Path:
    directory = _ca(tmp_path)
    mcp_gateway_certs.issue(directory, domain, repo_root=OUTSIDE)
    return directory


def _openssl(*args: str) -> str:
    return subprocess.run(["openssl", *args], capture_output=True, text=True, check=True).stdout


def test_issue_signs_the_wildcard_of_the_domain_with_the_platform_ca(tmp_path) -> None:
    directory = _ca(tmp_path)
    ca_before = (directory / certs.CA).read_text()
    internal_before = (directory / certs.INTERNAL).read_text()

    certificate, key = mcp_gateway_certs.issue(directory, DOMAIN, repo_root=OUTSIDE)

    assert (certificate.name, key.name) == ("mcp-gateway.pem", "mcp-gateway-key.pem")
    assert (directory / certs.CA).read_text() == ca_before
    assert (directory / certs.INTERNAL).read_text() == internal_before
    assert "OK" in _openssl("verify", "-CAfile", str(directory / certs.CA), str(certificate))
    text = _openssl("x509", "-in", str(certificate), "-noout", "-text")
    assert f"DNS:*.{DOMAIN}" in text
    assert "TLS Web Server Authentication" in text
    for host in HOSTS.values():
        checked = _openssl("x509", "-in", str(certificate), "-noout", "-checkhost", host)
        assert "does match certificate" in checked
    assert key.stat().st_mode & 0o077 == 0


def test_issue_refuses_the_repository_a_missing_domain_and_an_existing_certificate(
    tmp_path,
) -> None:
    with pytest.raises(CertificateError, match="inside the repository"):
        mcp_gateway_certs.issue(Path(__file__).parent / "tls", DOMAIN)
    directory = _ca(tmp_path)
    with pytest.raises(CertificateError, match="Pass --domain"):
        mcp_gateway_certs.issue(directory, "", repo_root=OUTSIDE)

    mcp_gateway_certs.issue(directory, DOMAIN, repo_root=OUTSIDE)
    with pytest.raises(CertificateError, match="Pass --force"):
        mcp_gateway_certs.issue(directory, DOMAIN, repo_root=OUTSIDE)
    mcp_gateway_certs.issue(directory, DOMAIN, force=True, repo_root=OUTSIDE)


def test_issue_refuses_a_directory_without_the_platform_ca(tmp_path) -> None:
    with pytest.raises(CertificateError, match="temporal-certs generate --internal-name"):
        mcp_gateway_certs.issue(tmp_path, DOMAIN, repo_root=OUTSIDE)


@pytest.mark.parametrize(
    "eks",
    [
        {"webhook_certificate_arn": WEBHOOK_ARN, "mcp_gateway_certificate_arn": WEBHOOK_ARN},
        {"webhook_certificate_arn": WEBHOOK_ARN, "mcp_gateway_certificate_arn": ""},
        {"webhook_certificate_arn": WEBHOOK_ARN},
        {},
    ],
    ids=["shared-with-the-webhook", "untracked", "absent", "no-output"],
)
def test_import_never_targets_the_webhook_certificate(tmp_path, eks) -> None:
    directory = _issued(tmp_path)
    acm = _FakeAcm()

    arn, replaced = mcp_gateway_certs.import_certificate(
        directory, {**eks, "mcp_gateway_hosts": HOSTS}, acm
    )

    assert (arn, replaced) == ("arn:aws:acm:us-east-1:1:new", False)
    [request] = acm.requests
    assert "CertificateArn" not in request
    assert WEBHOOK_ARN not in repr(request)


def test_a_reimport_goes_to_the_tracked_gateway_arn_with_the_ca_as_its_chain(tmp_path) -> None:
    directory = _issued(tmp_path)
    acm = _FakeAcm()
    eks = {
        "webhook_certificate_arn": WEBHOOK_ARN,
        "mcp_gateway_certificate_arn": GATEWAY_ARN,
        "mcp_gateway_hosts": HOSTS,
    }

    arn, replaced = mcp_gateway_certs.import_certificate(directory, eks, acm)

    assert (arn, replaced) == (GATEWAY_ARN, True)
    [request] = acm.requests
    assert request["CertificateArn"] == GATEWAY_ARN
    assert request["Certificate"] == (directory / mcp_gateway_certs.CERTIFICATE).read_bytes()
    assert request["PrivateKey"] == (directory / mcp_gateway_certs.KEY).read_bytes()
    assert request["CertificateChain"] == (directory / certs.CA).read_bytes()


def test_import_refuses_a_certificate_that_misses_a_tenant_host(tmp_path) -> None:
    directory = _issued(tmp_path, domain="other.internal")
    acm = _FakeAcm()

    with pytest.raises(CertificateError, match=f"mcp-alpha.{DOMAIN}, mcp-beta.{DOMAIN}"):
        mcp_gateway_certs.import_certificate(directory, {"mcp_gateway_hosts": HOSTS}, acm)
    assert acm.requests == []


def test_import_refuses_a_directory_without_the_certificate(tmp_path) -> None:
    directory = _ca(tmp_path)

    with pytest.raises(CertificateError, match="Run issue first"):
        mcp_gateway_certs.import_certificate(directory, {}, _FakeAcm())


def test_issue_defaults_the_domain_to_the_eks_output(tmp_path, monkeypatch, capsys) -> None:
    directory = _ca(tmp_path)
    monkeypatch.setattr(mcp_gateway_certs, "eks_output", lambda env: {"mcp_gateway_domain": DOMAIN})

    assert mcp_gateway_certs.main(["issue", "--dir", str(directory)]) == 0

    assert f"for *.{DOMAIN}" in capsys.readouterr().out
    text = _openssl(
        "x509", "-in", str(directory / mcp_gateway_certs.CERTIFICATE), "-noout", "-text"
    )
    assert f"DNS:*.{DOMAIN}" in text
