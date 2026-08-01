"""Establish the configured Bedrock connectivity path."""

from __future__ import annotations

import sys

from scripts.config import get_settings
from scripts.connectivity_providers import get_provider, status_tunnel, stop_tunnel


def start(detach: bool = False) -> int:
    settings = get_settings()
    try:
        provider = get_provider(settings)
    except ValueError as exc:
        print(f"[tunnel] FAIL: {exc}", file=sys.stderr)
        return 2

    print("[tunnel] Bedrock connectivity path")
    print(f"[tunnel]   provider    : {provider.key}")
    print(f"[tunnel]   profile     : {settings.aws_profile}")
    print(f"[tunnel]   region      : {settings.aws_region}")
    print(f"[tunnel]   path        : {provider.summary(settings)}")
    return provider.start(settings, detach=detach)


if __name__ == "__main__":
    args = sys.argv[1:]
    command = args[0] if args else "start"
    if command == "start":
        raise SystemExit(start(detach="--detach" in args))
    if command == "stop":
        raise SystemExit(stop_tunnel())
    if command == "status":
        raise SystemExit(status_tunnel())
    print(f"unknown command: {command}", file=sys.stderr)
    raise SystemExit(2)
