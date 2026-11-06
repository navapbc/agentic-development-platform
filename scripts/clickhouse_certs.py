"""Issue and store the ClickHouse server certificate.

`issue` signs a server certificate for the ClickHouse name and the loopback address, which the
task's health check connects to, with the CA in a directory that
`inv temporal-certs generate --internal-name` wrote, the CA the platform clients already trust
through PLATFORM_CA_PEM. `put` checks the certificate against that CA, its key, its name, and its
expiry, and writes the certificate and key to the Secrets Manager secret the `container-services`
module creates. Certificate material never passes through Terraform.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from services import process

from scripts.temporal_certs import (
    COMPOSITION_ROOT,
    SERVER_AUTH,
    CertificateError,
    _check_leaf,
    issue_server_certificate,
)

CERTIFICATE = "clickhouse.pem"
KEY = "clickhouse-key.pem"
DEFAULT_NAME = "clickhouse.data.platform.internal"
LOOPBACK = "127.0.0.1"


def put(directory: Path, clickhouse: dict[str, Any] | None, secretsmanager: Any) -> str:
    """Check the ClickHouse certificate in `directory` and write it to its secret."""
    if not clickhouse or not clickhouse.get("tls_secret_arn"):
        raise CertificateError(
            "The container_clickhouse output has no tls_secret_arn. Enable the container "
            "services with create_langfuse_bucket and create_clickhouse_storage, and apply first."
        )
    directory = directory.expanduser().resolve()
    missing = [name for name in ("ca.pem", CERTIFICATE, KEY) if not (directory / name).exists()]
    if missing:
        raise CertificateError(f"{directory} is missing {', '.join(missing)}.")
    _check_leaf(directory, CERTIFICATE, KEY, (SERVER_AUTH,), clickhouse["server_name"])
    secretsmanager.put_secret_value(
        SecretId=clickhouse["tls_secret_arn"],
        SecretString=json.dumps(
            {
                "certificate": (directory / CERTIFICATE).read_text(encoding="utf-8"),
                "private_key": (directory / KEY).read_text(encoding="utf-8"),
            }
        ),
    )
    return f"wrote the certificate for {clickhouse['server_name']} to its secret"


def _terraform_clickhouse(env: dict[str, str]) -> dict[str, Any] | None:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "container_clickhouse"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise CertificateError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue_parser = commands.add_parser("issue", help="Issue the certificate from an existing CA.")
    issue_parser.add_argument("--dir", required=True, type=Path)
    issue_parser.add_argument("--name", default=DEFAULT_NAME)
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
                [args.name],
                ip_addresses=(LOOPBACK,),
                days=args.days,
                force=args.force,
            )
            print(f"[clickhouse-certs] wrote {CERTIFICATE} and {KEY} to {written[0].parent}")
            return 0

        from scripts.aws_clients import configure_aws_fips
        from scripts.config import get_settings

        settings = get_settings()
        configure_aws_fips(settings)
        import boto3

        session = boto3.Session(region_name=args.region or settings.aws_region)
        action = put(
            args.dir, _terraform_clickhouse(dict(os.environ)), session.client("secretsmanager")
        )
        print(f"[clickhouse-certs] {action}")
        print("[clickhouse-certs] run inv platform-deploy to restart ClickHouse with it")
        return 0
    except CertificateError as exc:
        print(f"[clickhouse-certs] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
