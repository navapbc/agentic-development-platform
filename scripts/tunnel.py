"""Jump-host tunnel to the Bedrock runtime VPC endpoint via SSM port forwarding."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from scripts.config import get_settings


def _resolve_connect_script(configured: str) -> Path | None:
    candidate = configured or os.environ.get("JUMPHOST_CONNECT_SCRIPT", "")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    return path if path.is_file() else None


def start(detach: bool = False) -> int:
    settings = get_settings()
    script = _resolve_connect_script(settings.jumphost_connect_script)

    print("[tunnel] Bedrock jump-host path")
    print(f"[tunnel]   profile     : {settings.aws_profile}")
    print(f"[tunnel]   region      : {settings.aws_region}")
    print(f"[tunnel]   jumphost    : {settings.jumphost_name} (env {settings.jumphost_env})")
    print(f"[tunnel]   remote host : {settings.bedrock_runtime_host}:443")
    print(f"[tunnel]   local port  : {settings.bedrock_tunnel_local_port}")

    if script is None:
        print("[tunnel] FAIL: connect_to_jumphost.sh not found.", file=sys.stderr)
        print(
            "[tunnel] Set JUMPHOST_CONNECT_SCRIPT to the path of connect_to_jumphost.sh.",
            file=sys.stderr,
        )
        return 2

    forwarding = f"{settings.bedrock_tunnel_local_port}:{settings.bedrock_runtime_host}:443"
    cmd = [str(script), "-n", settings.jumphost_name, "-e", settings.jumphost_env, "-p", forwarding]
    env = {**os.environ, "AWS_PROFILE": settings.aws_profile, "AWS_REGION": settings.aws_region}

    print(f"[tunnel] exec: {' '.join(cmd)}")
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "start":
        raise SystemExit(start(detach="--detach" in args))
    print(f"unknown command: {args[0]}", file=sys.stderr)
    raise SystemExit(2)
