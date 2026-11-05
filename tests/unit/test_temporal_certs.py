"""Generating, checking, and storing the Temporal TLS certificates."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from scripts import temporal_certs as certs
from services import process

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

FRONTEND_NAMES = ["frontend.temporal.internal"]
INTERNODE_NAMES = ["internode.temporal.internal"]
TRACKED = "arn:aws:acm:us-east-1:111111111111:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRETS_TLS = {
    "frontend_certificate_source": "secrets",
    "ca_secret_arn": "arn:ca",
    "internode_secret_arn": "arn:internode",
    "frontend_secret_arn": "arn:frontend",
    "frontend_acm_certificate_arn": "",
    "internode_acm_certificate_arn": "",
    "frontend_server_name": FRONTEND_NAMES[0],
    "internode_server_name": INTERNODE_NAMES[0],
}
ACM_FRONTEND_TLS = {
    **SECRETS_TLS,
    "frontend_certificate_source": "acm",
    "frontend_secret_arn": None,
    "frontend_acm_certificate_arn": "arn:aws:acm:us-east-1:111111111111:certificate/f",
}


def _generate(out: Path, **options) -> list[Path]:
    return certs.generate(
        out,
        frontend_names=FRONTEND_NAMES,
        internode_names=INTERNODE_NAMES,
        repo_root=Path("/nonexistent-repository"),
        **options,
    )


@pytest.fixture(scope="module")
def issued(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("issued") / "tls"
    _generate(out)
    return out


def _copy(issued: Path, tmp_path: Path, *, frontend: bool = True) -> Path:
    directory = tmp_path / "copy"
    directory.mkdir()
    names = [certs.CA, certs.INTERNODE, certs.INTERNODE_KEY]
    names += [certs.FRONTEND, certs.FRONTEND_KEY] if frontend else []
    for name in names:
        shutil.copy(issued / name, directory / name)
    return directory


def test_generate_issues_certificates_that_verify_against_the_ca(issued) -> None:
    result = process.run(
        ["openssl", "verify", "-CAfile", str(issued / certs.CA), str(issued / certs.FRONTEND),
         str(issued / certs.INTERNODE)],
        timeout=30, capture_output=True, text=True,
    )  # fmt: skip

    assert result.returncode == 0, result.stderr


def test_generate_writes_every_file_readable_by_the_owner_only(issued) -> None:
    for path in issued.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_the_internode_certificate_serves_and_authenticates_clients(issued) -> None:
    text = certs._certificate_text(issued / certs.INTERNODE)

    assert certs.SERVER_AUTH in text and certs.CLIENT_AUTH in text
    assert "DNS:internode.temporal.internal" in text


def test_the_frontend_certificate_names_the_frontend(issued) -> None:
    text = certs._certificate_text(issued / certs.FRONTEND)

    assert "DNS:frontend.temporal.internal" in text
    assert certs.CLIENT_AUTH not in text


def test_generate_refuses_to_overwrite_without_force(issued) -> None:
    with pytest.raises(certs.CertificateError, match="Pass --force"):
        _generate(issued)


def test_generate_replaces_the_certificates_with_force(tmp_path) -> None:
    out = tmp_path / "tls"
    _generate(out)
    before = (out / certs.CA).read_text()

    _generate(out, force=True)

    assert (out / certs.CA).read_text() != before


def test_generate_refuses_a_directory_inside_the_repository(tmp_path) -> None:
    with pytest.raises(certs.CertificateError, match="inside the repository"):
        certs.generate(
            tmp_path / "keys",
            frontend_names=FRONTEND_NAMES,
            internode_names=INTERNODE_NAMES,
            repo_root=tmp_path,
        )


def test_generate_skips_the_frontend_pair_on_request(tmp_path) -> None:
    written = _generate(tmp_path / "tls", frontend=False)

    assert {path.name for path in written} == {
        certs.CA, certs.CA_KEY, certs.INTERNODE, certs.INTERNODE_KEY
    }  # fmt: skip


def test_load_refuses_a_key_that_belongs_to_another_certificate(issued, tmp_path) -> None:
    directory = _copy(issued, tmp_path)
    shutil.copy(issued / certs.FRONTEND_KEY, directory / certs.INTERNODE_KEY)

    with pytest.raises(certs.CertificateError, match="is not the private key"):
        certs.load(directory, frontend=True)


def test_load_refuses_an_internode_certificate_without_client_authentication(
    issued, tmp_path
) -> None:
    directory = _copy(issued, tmp_path)
    shutil.copy(issued / certs.FRONTEND, directory / certs.INTERNODE)
    shutil.copy(issued / certs.FRONTEND_KEY, directory / certs.INTERNODE_KEY)

    with pytest.raises(certs.CertificateError, match="TLS Web Client Authentication"):
        certs.load(directory, frontend=True)


def test_load_refuses_a_certificate_from_another_ca(issued, tmp_path) -> None:
    other = tmp_path / "other"
    _generate(other)
    directory = _copy(issued, tmp_path)
    shutil.copy(other / certs.CA, directory / certs.CA)

    with pytest.raises(certs.CertificateError, match="openssl verify failed"):
        certs.load(directory, frontend=True)


def test_load_refuses_frontend_files_when_acm_serves_the_frontend(issued, tmp_path) -> None:
    directory = _copy(issued, tmp_path)

    with pytest.raises(certs.CertificateError, match="presents the ACM certificate"):
        certs.load(directory, frontend=False)


def test_load_names_the_missing_files(issued, tmp_path) -> None:
    directory = _copy(issued, tmp_path, frontend=False)

    with pytest.raises(certs.CertificateError, match="frontend.pem, frontend-key.pem"):
        certs.load(directory, frontend=True)


class _SecretsManager:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        self.values[SecretId] = SecretString


class _Acm:
    def __init__(self, held: dict[str, str] | None = None) -> None:
        self.held = held or {}
        self.imports: list[dict] = []

    def import_certificate(self, **request):
        self.imports.append(request)
        arn = request.get("CertificateArn", TRACKED)
        self.held[arn] = request["Certificate"].decode()
        return {"CertificateArn": arn}

    def get_certificate(self, CertificateArn: str):
        return {"Certificate": self.held[CertificateArn]}


def test_put_writes_the_ca_and_both_pairs(issued) -> None:
    secretsmanager = _SecretsManager()

    certs.put(issued, SECRETS_TLS, secretsmanager, _Acm())

    assert secretsmanager.values["arn:ca"] == (issued / certs.CA).read_text()
    internode = json.loads(secretsmanager.values["arn:internode"])
    assert internode == {
        "certificate": (issued / certs.INTERNODE).read_text(),
        "private_key": (issued / certs.INTERNODE_KEY).read_text(),
    }
    assert (
        json.loads(secretsmanager.values["arn:frontend"])["certificate"]
        == (issued / certs.FRONTEND).read_text()
    )


def test_put_writes_no_frontend_secret_when_acm_serves_the_frontend(issued, tmp_path) -> None:
    secretsmanager = _SecretsManager()

    certs.put(_copy(issued, tmp_path, frontend=False), ACM_FRONTEND_TLS, secretsmanager, _Acm())

    assert set(secretsmanager.values) == {"arn:ca", "arn:internode"}


@pytest.mark.parametrize("setting", ["frontend_server_name", "internode_server_name"])
def test_put_refuses_a_certificate_without_the_configured_name_and_writes_nothing(
    issued, setting
) -> None:
    secretsmanager = _SecretsManager()

    with pytest.raises(certs.CertificateError, match="does not name temporal.example.gov"):
        certs.put(issued, {**SECRETS_TLS, setting: "temporal.example.gov"}, secretsmanager, _Acm())

    assert secretsmanager.values == {}


def test_put_imports_a_new_certificate_and_names_the_variable(issued) -> None:
    acm = _Acm()

    actions = certs.put(issued, SECRETS_TLS, _SecretsManager(), acm, import_to_acm=True)

    [request] = acm.imports
    assert "CertificateArn" not in request
    assert request["CertificateChain"] == (issued / certs.CA).read_bytes()
    assert any("temporal_internode_acm_certificate_arn" in action for action in actions)


def test_put_reimports_into_the_tracked_certificate(issued) -> None:
    acm = _Acm()

    certs.put(
        issued,
        {**SECRETS_TLS, "internode_acm_certificate_arn": TRACKED},
        _SecretsManager(),
        acm,
        import_to_acm=True,
    )

    assert acm.imports[0]["CertificateArn"] == TRACKED


def test_put_accepts_the_certificate_acm_tracks(issued) -> None:
    acm = _Acm({TRACKED: (issued / certs.INTERNODE).read_text()})
    secretsmanager = _SecretsManager()

    certs.put(
        issued, {**SECRETS_TLS, "internode_acm_certificate_arn": TRACKED}, secretsmanager, acm
    )

    assert "arn:internode" in secretsmanager.values


def test_put_refuses_a_certificate_acm_does_not_track_and_writes_nothing(issued, tmp_path) -> None:
    other = tmp_path / "other"
    _generate(other)
    acm = _Acm({TRACKED: (other / certs.INTERNODE).read_text()})
    secretsmanager = _SecretsManager()

    with pytest.raises(certs.CertificateError, match="is not the certificate ACM tracks"):
        certs.put(
            issued, {**SECRETS_TLS, "internode_acm_certificate_arn": TRACKED}, secretsmanager, acm
        )

    assert secretsmanager.values == {}


def test_put_refuses_before_the_secrets_exist(issued) -> None:
    with pytest.raises(certs.CertificateError, match="create_temporal_control_plane"):
        certs.put(issued, {**SECRETS_TLS, "ca_secret_arn": None}, _SecretsManager(), _Acm())


class _FailingSecretsManager(_SecretsManager):
    def __init__(self, fails_on: str) -> None:
        super().__init__()
        self.fails_on = fails_on

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        if SecretId == self.fails_on:
            raise RuntimeError("Secrets Manager throttled the request.")
        super().put_secret_value(SecretId=SecretId, SecretString=SecretString)


@pytest.mark.parametrize("fails_on", ["arn:internode", "arn:frontend"])
def test_a_failed_leaf_write_leaves_the_ca_unrotated(issued, fails_on) -> None:
    secretsmanager = _FailingSecretsManager(fails_on)

    with pytest.raises(RuntimeError):
        certs.put(issued, SECRETS_TLS, secretsmanager, _Acm())

    assert "arn:ca" not in secretsmanager.values


def test_a_failed_write_names_the_secrets_already_written(issued) -> None:
    with pytest.raises(RuntimeError) as failure:
        certs.put(issued, SECRETS_TLS, _FailingSecretsManager("arn:frontend"), _Acm())

    assert "wrote the internode certificate and key before the failure" in " ".join(
        failure.value.__notes__
    )


def test_put_checks_every_secret_arn_before_the_first_write(issued) -> None:
    secretsmanager = _SecretsManager()

    with pytest.raises(certs.CertificateError):
        certs.put(issued, {**SECRETS_TLS, "internode_secret_arn": None}, secretsmanager, _Acm())

    assert secretsmanager.values == {}


def test_generate_issues_a_ui_certificate_on_request(tmp_path) -> None:
    out = tmp_path / "tls"
    written = _generate(out, ui_names=["temporal-ui.example.internal"])

    assert {certs.UI, certs.UI_KEY} <= {path.name for path in written}
    text = certs._certificate_text(out / certs.UI)
    assert "DNS:temporal-ui.example.internal" in text and certs.CLIENT_AUTH not in text
