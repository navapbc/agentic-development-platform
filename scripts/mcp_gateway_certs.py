"""Issue the MCP gateway certificate from the platform CA and import it into ACM.

`issue` signs a server certificate for `*.<eks_mcp_gateway_domain>`, which covers each tenant's
gateway host `mcp-<tenant>.<eks_mcp_gateway_domain>`, with the CA in a directory that
`inv temporal-certs generate --internal-name` wrote. MCP clients trust that CA through
container_platform_ca_file. `import` checks the certificate against that CA and against each
tenant's gateway host the `eks` output lists, and imports it into ACM with the CA as its chain. It
re-imports into the `mcp_gateway_certificate_arn` the `eks` output tracks, so a renewal keeps the
ARN every gateway load balancer presents, and creates a new ARN when that one is the inbound
webhook certificate, which it never replaces.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

from scripts.eks_deploy import certificate_covers
from scripts.temporal_certs import (
    CA,
    REPO_ROOT,
    SERVER_AUTH,
    CertificateError,
    _certificate_text,
    _check_leaf,
    issue_server_certificate,
)
from scripts.webhook_certs import eks_output

CERTIFICATE = "mcp-gateway.pem"
KEY = "mcp-gateway-key.pem"
LOG = "[mcp-gateway-certs]"

_DNS_NAME = re.compile(r"DNS:([^\s,]+)")


def issue(
    directory: Path,
    domain: str,
    *,
    days: int = 397,
    force: bool = False,
    repo_root: Path = REPO_ROOT,
) -> list[Path]:
    """Sign a certificate for `*.<domain>` with the platform CA in `directory`."""
    if not domain:
        raise CertificateError(
            "The eks output has no mcp_gateway_domain. Pass --domain with the value of "
            "eks_mcp_gateway_domain."
        )
    return issue_server_certificate(
        directory, CERTIFICATE, KEY, [f"*.{domain}"], days=days, force=force, repo_root=repo_root
    )


def import_certificate(directory: Path, eks: dict[str, Any], acm: Any) -> tuple[str, bool]:
    """Import the certificate into ACM and return its ARN and whether it replaced a tracked one."""
    directory = directory.expanduser().resolve()
    missing = [name for name in (CA, CERTIFICATE, KEY) if not (directory / name).exists()]
    if missing:
        raise CertificateError(f"{directory} is missing {', '.join(missing)}. Run issue first.")
    _check_leaf(directory, CERTIFICATE, KEY, (SERVER_AUTH,))
    names = _DNS_NAME.findall(_certificate_text(directory / CERTIFICATE))
    hosts = (eks.get("mcp_gateway_hosts") or {}).values()
    uncovered = sorted(host for host in hosts if not certificate_covers(names, host))
    if uncovered:
        raise CertificateError(
            f"{CERTIFICATE} names {', '.join(names)}, which do not cover {', '.join(uncovered)}. "
            "Issue it for the eks_mcp_gateway_domain the tenants' gateway hosts are named under."
        )
    tracked = eks.get("mcp_gateway_certificate_arn") or ""
    if tracked == (eks.get("webhook_certificate_arn") or ""):
        tracked = ""
    request: dict[str, Any] = {
        "Certificate": (directory / CERTIFICATE).read_bytes(),
        "PrivateKey": (directory / KEY).read_bytes(),
        "CertificateChain": (directory / CA).read_bytes(),
    }
    if tracked:
        request["CertificateArn"] = tracked
    return acm.import_certificate(**request)["CertificateArn"], bool(tracked)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["issue", "import"])
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument(
        "--domain", default="", help="issue: the gateway domain. Defaults to the eks output's."
    )
    parser.add_argument("--days", type=int, default=397)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--region", default="")
    args = parser.parse_args(argv)

    try:
        if args.action == "issue":
            domain = args.domain or eks_output(dict(os.environ)).get("mcp_gateway_domain") or ""
            issue(args.dir, domain, days=args.days, force=args.force)
            print(f"{LOG} wrote {CERTIFICATE} and {KEY} for *.{domain} to {args.dir}")
            return 0

        from scripts.aws_clients import configure_aws_fips
        from scripts.config import get_settings

        settings = get_settings()
        configure_aws_fips(settings)
        import boto3

        acm = boto3.Session(region_name=args.region or settings.aws_region).client("acm")
        arn, replaced = import_certificate(args.dir, eks_output(dict(os.environ)), acm)
    except CertificateError as exc:
        print(f"{LOG} {exc}")
        return 1
    if replaced:
        print(f"{LOG} re-imported the certificate into {arn}")
    else:
        print(
            f"{LOG} imported the certificate as {arn}. Set "
            f'eks_mcp_gateway_certificate_arn = "{arn}" in the tfvars file, apply, and run '
            "inv eks-deploy"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
