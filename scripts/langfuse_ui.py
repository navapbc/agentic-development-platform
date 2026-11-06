"""Forward a local port to Langfuse on the internal load balancer through the jump-host.

The internal load balancer admits the jump-host task group. This starts a jump-host task with
the connect script the Bedrock tunnel uses and forwards the local port to the Langfuse name on
443 until it is interrupted. Langfuse redirects sign-in to its public URL, so the local port
must be the one in that URL.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.parse import urlparse

from services import process

from scripts.connectivity_providers import JumphostConfigError, jumphost_command
from scripts.temporal_certs import COMPOSITION_ROOT


class LangfuseUnavailableError(Exception):
    """The composition root runs no Langfuse to forward to."""


def forward_target(langfuse: dict[str, Any] | None) -> tuple[str, int]:
    """Return the internal Langfuse host and the local port its public URL names."""
    if not langfuse:
        raise LangfuseUnavailableError(
            "The container services run no Langfuse. Set the container_langfuse_* images and "
            "container_langfuse_admin_email, and apply private-bedrock."
        )
    host = urlparse(langfuse["url"]).hostname
    public = urlparse(langfuse["public_url"])
    if not host or public.hostname != host:
        raise LangfuseUnavailableError(
            f"The public URL {langfuse['public_url']} does not name {host}, so a forward to it "
            "cannot complete the Langfuse sign-in."
        )
    return host, public.port or 443


def _terraform_langfuse(env: dict[str, str]) -> dict[str, Any] | None:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "container_langfuse"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise LangfuseUnavailableError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    from scripts.config import get_settings

    settings = get_settings()
    env = {**os.environ, "AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}
    try:
        host, port = forward_target(_terraform_langfuse(env))
        cmd = jumphost_command(settings, f"{port}:{host}:443")
    except (LangfuseUnavailableError, JumphostConfigError) as exc:
        print(f"[langfuse-ui] {exc}")
        return 1
    print(
        f"[langfuse-ui] forwarding localhost:{port} to {host}:443. Open https://{host}:{port} "
        f"with {host} mapped to 127.0.0.1 and the platform CA trusted."
    )
    process.replace_process(cmd, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
