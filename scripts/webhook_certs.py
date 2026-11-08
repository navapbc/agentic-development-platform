"""Issue a self-signed certificate for the inbound webhook load balancer and import it into ACM.

An environment without a public domain has no certificate a public CA would sign, so the
internet-facing load balancer of the inbound webhook presents a self-signed certificate. The
default name is the wildcard that covers every load balancer host name in the region,
`*.<region>.elb.amazonaws.com`, because the load balancer's name exists only once the controller
creates it. A sender delivers to it only with TLS verification turned off for that webhook.

`issue` writes the certificate and its key to a directory outside the repository. `import`
imports them into ACM, into the ARN `eks_webhook_certificate_arn` already names when it is set,
so a renewal keeps the ARN the load balancer uses.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from services import process

from scripts.temporal_certs import (
    COMPOSITION_ROOT,
    REPO_ROOT,
    SERVER_AUTH,
    CertificateError,
    _certificate_text,
    _inside,
    _new_key,
    _openssl,
)

CERTIFICATE = "webhook.pem"
KEY = "webhook-key.pem"

_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
x509_extensions = leaf

[dn]
CN = {name}

[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:{name}
subjectKeyIdentifier = hash
"""


def default_name(region: str) -> str:
    return f"*.{region}.elb.amazonaws.com"


def issue(
    directory: Path, name: str, *, days: int = 397, force: bool = False, repo_root: Path = REPO_ROOT
) -> list[Path]:
    """Write a self-signed server certificate for `name` and its P-256 key into `directory`."""
    directory = directory.expanduser().resolve()
    if _inside(directory, repo_root.resolve()):
        raise CertificateError(
            f"{directory} is inside the repository. Write the certificate to a directory outside "
            "it, such as ~/.agentic-platform/webhook-tls, so no key can be committed."
        )
    targets = [directory / CERTIFICATE, directory / KEY]
    existing = [path.name for path in targets if path.exists()]
    if existing and not force:
        raise CertificateError(
            f"{', '.join(existing)} already exist in {directory}. Pass --force to replace them."
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous_umask = os.umask(0o077)
    try:
        _new_key(directory / KEY)
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as config:
            config.write(_CONFIG.format(name=name))
        try:
            _openssl(
                "req",
                "-new",
                "-x509",
                "-config",
                config.name,
                "-key",
                str(directory / KEY),
                "-days",
                str(days),
                "-out",
                str(directory / CERTIFICATE),
            )
        finally:
            os.unlink(config.name)
    finally:
        os.umask(previous_umask)
    return targets


def import_certificate(directory: Path, tracked_arn: str, acm: Any) -> str:
    """Import the certificate into ACM, into `tracked_arn` when one is set, and return its ARN."""
    directory = directory.expanduser().resolve()
    missing = [name for name in (CERTIFICATE, KEY) if not (directory / name).exists()]
    if missing:
        raise CertificateError(f"{directory} is missing {', '.join(missing)}. Run issue first.")
    if SERVER_AUTH not in _certificate_text(directory / CERTIFICATE):
        raise CertificateError(f"{CERTIFICATE} lacks the extended key usage {SERVER_AUTH}.")
    request: dict[str, Any] = {
        "Certificate": (directory / CERTIFICATE).read_bytes(),
        "PrivateKey": (directory / KEY).read_bytes(),
    }
    if tracked_arn:
        request["CertificateArn"] = tracked_arn
    return acm.import_certificate(**request)["CertificateArn"]


def _tracked_arn(env: dict[str, str]) -> str:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "eks"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return ""
    return (json.loads(result.stdout or "null") or {}).get("webhook_certificate_arn") or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["issue", "import"])
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--name", default="", help="Host name. Defaults to the region's ELB name.")
    parser.add_argument("--days", type=int, default=397)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    from scripts.config import get_settings

    settings = get_settings()
    try:
        if args.action == "issue":
            name = args.name or default_name(settings.aws_region)
            issue(args.dir, name, days=args.days, force=args.force)
            print(f"[webhook-certs] wrote {CERTIFICATE} and {KEY} for {name} to {args.dir}")
            return 0

        import boto3

        tracked = _tracked_arn(dict(os.environ))
        acm = boto3.Session(region_name=settings.aws_region).client("acm")
        arn = import_certificate(args.dir, tracked, acm)
    except CertificateError as exc:
        print(f"[webhook-certs] {exc}")
        return 1
    if tracked:
        print(f"[webhook-certs] re-imported the certificate into {arn}")
    else:
        print(
            f"[webhook-certs] imported the certificate as {arn}. Set "
            f'eks_webhook_certificate_arn = "{arn}" in the tfvars file and apply'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
