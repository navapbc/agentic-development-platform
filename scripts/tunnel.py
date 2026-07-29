"""Establish the configured Bedrock connectivity path."""

from __future__ import annotations

import sys

from scripts.config import get_settings
from scripts.connectivity_providers import get_provider


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
    return provider.start(settings)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "start":
        raise SystemExit(start(detach="--detach" in args))
    print(f"unknown command: {args[0]}", file=sys.stderr)
    raise SystemExit(2)
