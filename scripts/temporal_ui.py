"""Forward a local port to the Temporal UI load balancer through the jump-host.

The UI load balancer is internal and admits the jump-host task group. This starts a jump-host
task with the connect script the Bedrock tunnel uses and forwards the local port to the load
balancer on 443 until it is interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlparse

from services import process

from scripts.connectivity_providers import JumphostConfigError, jumphost_command
from scripts.temporal_certs import COMPOSITION_ROOT

DEFAULT_LOCAL_PORT = 8443


class UiUnavailableError(Exception):
    """The composition root has no UI load balancer to forward to."""


def ui_host(ui_url: str | None) -> str:
    host = urlparse(ui_url or "").hostname
    if not host:
        raise UiUnavailableError(
            "The control plane has no UI. Set temporal_ui_image and "
            "temporal_ui_acm_certificate_arn, and apply private-bedrock."
        )
    return host


def _terraform_ui_url(env: dict[str, str]) -> str | None:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "temporal_ui_url"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise UiUnavailableError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_LOCAL_PORT)
    args = parser.parse_args(argv)

    from scripts.config import get_settings

    settings = get_settings()
    env = {**os.environ, "AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    try:
        host = ui_host(_terraform_ui_url(env))
        cmd = jumphost_command(settings, f"{args.port}:{host}:443")
    except (UiUnavailableError, JumphostConfigError) as exc:
        print(f"[temporal-ui] {exc}")
        return 1
    print(
        f"[temporal-ui] forwarding localhost:{args.port} to {host}:443. Open "
        f"https://<UI certificate name>:{args.port} with that name mapped to 127.0.0.1."
    )
    process.replace_process(cmd, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
