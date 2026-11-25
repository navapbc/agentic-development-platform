"""Issue and store the Slack posting service's server certificate for EKS.

`issue` signs a server certificate for the posting service's in-cluster name and the loopback
address, which its health probe connects to, with the CA in a directory that
`inv temporal-certs generate --internal-name` wrote, the CA the platform's PLATFORM_CA_PEM carries
and each tenant worker trusts through the slack-poster-ca ConfigMap. `put` checks the certificate
against that CA, its key, its name, and its expiry, and writes the certificate and key to the
secret store's slack_poster_tls secret, which the posting service's SecretProviderClass mounts.
Certificate material never passes through Terraform.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from services.server_tls import LOOPBACK

from scripts.eks_render import PLATFORM_NAMESPACE
from scripts.slack_poster_tls import CERTIFICATE, KEY
from scripts.temporal_certs import (
    CA,
    SERVER_AUTH,
    CertificateError,
    _check_leaf,
    issue_server_certificate,
    terraform_output,
)
from scripts.webhook_eks_render import SLACK_POSTER

SERVER_NAME = f"{SLACK_POSTER}.{PLATFORM_NAMESPACE}.svc.cluster.local"
STORE_KEY = "slack_poster_tls"


def put(directory: Path, secret_arns: dict[str, str] | None, secretsmanager: Any) -> str:
    """Check the posting service certificate in `directory` and write it to its secret."""
    arn = (secret_arns or {}).get(STORE_KEY)
    if not arn:
        raise CertificateError(
            f"The secret_arns output has no {STORE_KEY}. Set slack_shared_app and apply first."
        )
    directory = directory.expanduser().resolve()
    missing = [name for name in (CA, CERTIFICATE, KEY) if not (directory / name).exists()]
    if missing:
        raise CertificateError(f"{directory} is missing {', '.join(missing)}.")
    _check_leaf(directory, CERTIFICATE, KEY, (SERVER_AUTH,), SERVER_NAME)
    secretsmanager.put_secret_value(
        SecretId=arn,
        SecretString=json.dumps(
            {
                "certificate": (directory / CERTIFICATE).read_text(encoding="utf-8"),
                "private_key": (directory / KEY).read_text(encoding="utf-8"),
            }
        ),
    )
    return f"wrote the certificate for {SERVER_NAME} to {STORE_KEY}"


def _terraform_secret_arns(env: dict[str, str]) -> dict[str, str]:
    return json.loads(terraform_output("secret_arns", env, CertificateError)) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue_parser = commands.add_parser("issue", help="Issue the certificate from an existing CA.")
    issue_parser.add_argument("--dir", required=True, type=Path)
    issue_parser.add_argument("--days", type=int, default=397)
    issue_parser.add_argument("--force", action="store_true")
    put_parser = commands.add_parser("put", help="Check the certificate and write its secret.")
    put_parser.add_argument("--dir", required=True, type=Path)
    put_parser.add_argument("--region", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "issue":
            written = issue_server_certificate(
                args.dir,
                CERTIFICATE,
                KEY,
                [SERVER_NAME],
                ip_addresses=(LOOPBACK,),
                days=args.days,
                force=args.force,
            )
            print(f"[slack-poster-certs] wrote {CERTIFICATE} and {KEY} to {written[0].parent}")
            return 0

        from scripts.aws_clients import configure_aws_fips
        from scripts.config import get_settings

        settings = get_settings()
        configure_aws_fips(settings)
        import boto3

        session = boto3.Session(region_name=args.region or settings.aws_region)
        action = put(
            args.dir, _terraform_secret_arns(dict(os.environ)), session.client("secretsmanager")
        )
        print(f"[slack-poster-certs] {action}")
        print("[slack-poster-certs] the secret store restarts slack-poster with it")
        return 0
    except CertificateError as exc:
        print(f"[slack-poster-certs] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
