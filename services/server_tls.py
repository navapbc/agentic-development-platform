"""TLS for the platform HTTP services behind the internal load balancer.

With `SERVER_TLS=true` a service issues itself a self-signed certificate when it starts and
serves HTTPS with it. The load balancer re-encrypts to the task and does not verify the task's
certificate, so the key is generated in the task and never leaves it. The certificate is also
written beside the key, so the container health probe can verify the service it calls.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import ssl
import sys
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

CERTIFICATE = "cert.pem"
KEY = "key.pem"
LOOPBACK = "127.0.0.1"
VALIDITY = datetime.timedelta(days=397)


def certificate_dir(configured: str = "") -> Path:
    return (
        Path(configured or os.environ.get("SERVER_TLS_DIR") or tempfile.gettempdir()) / "server-tls"
    )


def issue(directory: Path) -> tuple[Path, Path]:
    """Write a P-256 key and a self-signed certificate naming this host and the loopback address."""
    key = ec.generate_private_key(ec.SECP256R1())
    hostname = socket.gethostname()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + VALIDITY)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(hostname),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address(LOOPBACK)),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path, certificate_path = directory / KEY, directory / CERTIFICATE
    key_path.touch(mode=0o600, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return certificate_path, key_path


def server_context(directory: Path) -> ssl.SSLContext:
    certificate, key = issue(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    return context


def wrap(server, *, enabled: bool, configured_dir: str = "") -> None:
    """Serve `server` over TLS when enabled. The handshake runs on the handler's thread."""
    if not enabled:
        return
    context = server_context(certificate_dir(configured_dir))
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    report_other_errors = server.handle_error

    def handle_error(request, client_address) -> None:
        error = sys.exception()
        if isinstance(error, ssl.SSLError):
            log.warning("refused a TLS connection from %s: %s", client_address[0], error.reason)
            return
        report_other_errors(request, client_address)

    server.handle_error = handle_error


def probe_context(configured_dir: str = "") -> ssl.SSLContext:
    """Trust only the certificate this task issued, for the loopback health probe."""
    return ssl.create_default_context(cafile=certificate_dir(configured_dir) / CERTIFICATE)
