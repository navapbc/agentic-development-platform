"""Issuing and storing the ClickHouse server certificate."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from scripts import clickhouse_certs
from scripts import temporal_certs as certs

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

NAME = "clickhouse.data.platform.internal"
OUTPUT = {"tls_secret_arn": "arn:clickhouse-tls", "server_name": NAME}


class _Secrets:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        self.writes.append((SecretId, json.loads(SecretString)))


def _ca(tmp_path: Path) -> Path:
    out = tmp_path / "tls"
    certs.generate(
        out,
        frontend_names=["frontend.temporal.internal"],
        internode_names=["internode.temporal.internal"],
        frontend=False,
        repo_root=Path("/nonexistent-repository"),
    )
    return out


def _issue(directory: Path, name: str = NAME, **options) -> list[Path]:
    return certs.issue_server_certificate(
        directory,
        clickhouse_certs.CERTIFICATE,
        clickhouse_certs.KEY,
        [name],
        ip_addresses=(clickhouse_certs.LOOPBACK,),
        repo_root=Path("/nonexistent-repository"),
        **options,
    )


def test_the_certificate_comes_from_the_ca_already_in_the_directory(tmp_path) -> None:
    directory = _ca(tmp_path)
    ca_before = (directory / certs.CA).read_text()

    _issue(directory)

    assert (directory / certs.CA).read_text() == ca_before
    text = certs._certificate_text(directory / clickhouse_certs.CERTIFICATE)
    assert f"DNS:{NAME}" in text and "IP Address:127.0.0.1" in text
    secrets = _Secrets()
    clickhouse_certs.put(directory, OUTPUT, secrets)
    secret_id, value = secrets.writes[0]
    assert secret_id == "arn:clickhouse-tls"
    assert value["certificate"] == (directory / clickhouse_certs.CERTIFICATE).read_text()
    assert value["private_key"] == (directory / clickhouse_certs.KEY).read_text()


def test_issue_needs_the_ca_key(tmp_path) -> None:
    directory = _ca(tmp_path)
    (directory / certs.CA_KEY).unlink()

    with pytest.raises(certs.CertificateError, match="missing ca-key.pem"):
        _issue(directory)


def test_issue_refuses_to_overwrite_without_force(tmp_path) -> None:
    directory = _ca(tmp_path)
    _issue(directory)

    with pytest.raises(certs.CertificateError, match="already exist"):
        _issue(directory)
    _issue(directory, force=True)


def test_put_refuses_a_certificate_for_another_name(tmp_path) -> None:
    directory = _ca(tmp_path)
    _issue(directory, name="clickhouse.example.internal")
    secrets = _Secrets()

    with pytest.raises(certs.CertificateError, match=f"does not name {NAME}"):
        clickhouse_certs.put(directory, OUTPUT, secrets)
    assert secrets.writes == []


def test_put_refuses_a_certificate_from_another_ca(tmp_path) -> None:
    directory = _ca(tmp_path)
    _issue(directory)
    shutil.copy(_ca(tmp_path / "other") / certs.CA, directory / certs.CA)

    with pytest.raises(certs.CertificateError):
        clickhouse_certs.put(directory, OUTPUT, _Secrets())


def test_put_without_the_module_output_names_the_settings(tmp_path) -> None:
    with pytest.raises(certs.CertificateError, match="container_clickhouse output"):
        clickhouse_certs.put(tmp_path, None, _Secrets())
