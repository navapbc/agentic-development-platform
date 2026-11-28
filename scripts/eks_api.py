"""Forward a local port to the private EKS API through the jump-host and write its kubeconfig.

The cluster endpoint is private and admits the jump-host task group. This starts a jump-host
task, forwards the local port to the endpoint on 443, and writes a kubeconfig whose context
reaches the API through that port, verifies the API certificate against the endpoint's name, and
mints each token from the operator's AWS profile. It forwards until interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services import process

from scripts.connectivity_providers import JumphostConfigError, jumphost_command
from scripts.temporal_certs import COMPOSITION_ROOT

DEFAULT_LOCAL_PORT = 9443
CONTEXT = "agentic-platform-eks"
DEFAULT_KUBECONFIG = Path.home() / ".kube" / f"{CONTEXT}.json"
LOG = "[eks-api]"


class EksApiUnavailableError(Exception):
    """The composition root has no EKS cluster to forward to."""


def kubeconfig(
    *, cluster: str, host: str, ca_data: str, port: int, region: str, profile: str
) -> dict[str, Any]:
    """Return a kubeconfig that reaches the cluster API through the forwarded local port."""
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": cluster,
                "cluster": {
                    "server": f"https://127.0.0.1:{port}",
                    "tls-server-name": host,
                    "certificate-authority-data": ca_data,
                },
            }
        ],
        "users": [
            {
                "name": cluster,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "aws",
                        "args": ["eks", "get-token", "--cluster-name", cluster, "--region", region],
                        "env": [{"name": "AWS_PROFILE", "value": profile}],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
        "contexts": [{"name": CONTEXT, "context": {"cluster": cluster, "user": cluster}}],
        "current-context": CONTEXT,
    }


def _eks_output(env: dict[str, str]) -> dict[str, Any]:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "eks"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise EksApiUnavailableError(f"terraform output failed: {result.stderr.strip()}")
    eks = json.loads(result.stdout) or {}
    if not eks.get("cluster_name") or not eks.get("endpoint"):
        raise EksApiUnavailableError("The composition root has no EKS cluster. Set create_eks.")
    return eks


def _cluster_ca(cluster: str, region: str, profile: str) -> str:
    import boto3

    session = boto3.Session(profile_name=profile or None, region_name=region)
    described = session.client("eks").describe_cluster(name=cluster)
    return str(described["cluster"]["certificateAuthority"]["data"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    args = parser.parse_args(argv)

    from scripts.config import get_settings

    settings = get_settings()
    env = {**os.environ, "AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    try:
        eks = _eks_output(env)
        host = urlparse(eks["endpoint"]).hostname or ""
        cluster, region = eks["cluster_name"], eks.get("region") or settings.aws_region
        config = kubeconfig(
            cluster=cluster,
            host=host,
            ca_data=_cluster_ca(cluster, region, settings.aws_profile),
            port=args.port,
            region=region,
            profile=settings.aws_profile,
        )
        cmd = jumphost_command(settings, f"{args.port}:{host}:443")
    except (EksApiUnavailableError, JumphostConfigError) as exc:
        print(f"{LOG} {exc}")
        return 1
    args.kubeconfig.parent.mkdir(parents=True, exist_ok=True)
    args.kubeconfig.write_text(json.dumps(config, indent=2), encoding="utf-8")
    args.kubeconfig.chmod(0o600)
    print(
        f"{LOG} forwarding localhost:{args.port} to {host}:443. In another shell, run "
        f"`export KUBECONFIG={args.kubeconfig}`, then kubectl and the inv tasks that take "
        f"--context reach the cluster as `--context {CONTEXT}`."
    )
    process.replace_process(cmd, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
