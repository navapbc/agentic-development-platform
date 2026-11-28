"""The CA and the server certificate of the Slack posting service in a local cluster.

The local render serves the posting service a leaf issued by a CA the tenant workers trust. The CA
and the leaf are written once to a per-user state directory outside the repository and reused on
every render. The leaf is issued again when it is within RENEW_BEFORE of its expiry or names other
hosts than the render asks for, so the posting service rolls only when its certificate changes.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO_ROOT = Path(__file__).resolve().parent.parent
CA_VALIDITY = datetime.timedelta(days=3650)
LEAF_VALIDITY = datetime.timedelta(days=397)
RENEW_BEFORE = datetime.timedelta(days=30)
CA_CERTIFICATE = "ca.pem"
CA_KEY = "ca-key.pem"
CERTIFICATE = "slack-poster.pem"
KEY = "slack-poster-key.pem"


class TlsStateError(Exception):
    """The state directory cannot hold the posting service's certificates."""


@dataclass(frozen=True)
class Material:
    ca: str
    certificate: str
    key: str


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agentic-dev-platform" / "slack-poster-tls"


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def new_ca(
    common_name: str = "Agentic Dev Platform local Slack posting CA",
    *,
    now: datetime.datetime | None = None,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    now = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _san(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def issue_leaf(
    ca_key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    names: Sequence[str],
    *,
    now: datetime.datetime | None = None,
    validity: datetime.timedelta = LEAF_VALIDITY,
) -> tuple[str, str]:
    """Return the PEM certificate and key of a server leaf for the DNS names and IP addresses."""
    now = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + validity)
        .add_extension(x509.SubjectAlternativeName([_san(name) for name in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode(), _pem(key).decode()


def _names(certificate: x509.Certificate) -> list[str]:
    sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(entry.value) for entry in sans]


def _current_leaf(
    directory: Path, ca: x509.Certificate, names: Sequence[str], now: datetime.datetime
) -> Material | None:
    try:
        text = (directory / CERTIFICATE).read_text(encoding="utf-8")
        key = (directory / KEY).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    leaf = x509.load_pem_x509_certificate(text.encode())
    if (
        leaf.issuer != ca.subject
        or sorted(_names(leaf)) != sorted(names)
        or leaf.not_valid_after_utc - now <= RENEW_BEFORE
    ):
        return None
    return Material(ca.public_bytes(serialization.Encoding.PEM).decode(), text, key)


def _write(path: Path, data: str, mode: int) -> None:
    path.touch(mode=mode, exist_ok=True)
    path.chmod(mode)
    path.write_text(data, encoding="utf-8")


def local_material(
    names: Sequence[str],
    *,
    directory: Path | None = None,
    repo_root: Path = REPO_ROOT,
    now: datetime.datetime | None = None,
) -> Material:
    """Return the local CA and a leaf for the names, issuing either only when it is missing or
    the leaf is due for renewal or names other hosts."""
    directory = (directory or state_dir()).expanduser().resolve()
    if directory.is_relative_to(repo_root.resolve()):
        raise TlsStateError(
            f"{directory} is inside the repository. Keep the posting service's CA key outside it."
        )
    now = now or datetime.datetime.now(datetime.UTC)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        ca = x509.load_pem_x509_certificate((directory / CA_CERTIFICATE).read_bytes())
        ca_key = serialization.load_pem_private_key((directory / CA_KEY).read_bytes(), None)
    except FileNotFoundError:
        ca_key, ca = new_ca(now=now)
        _write(directory / CA_KEY, _pem(ca_key).decode(), 0o600)
        _write(
            directory / CA_CERTIFICATE, ca.public_bytes(serialization.Encoding.PEM).decode(), 0o644
        )
    if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
        raise TlsStateError(f"{directory / CA_KEY} is not an EC private key")
    current = _current_leaf(directory, ca, names, now)
    if current is not None:
        return current
    certificate, key = issue_leaf(ca_key, ca, names, now=now)
    _write(directory / KEY, key, 0o600)
    _write(directory / CERTIFICATE, certificate, 0o644)
    return Material(ca.public_bytes(serialization.Encoding.PEM).decode(), certificate, key)
