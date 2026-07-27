"""Validate the Bedrock path and local service reachability."""

from __future__ import annotations

import httpx

from scripts.config import get_settings, to_host_url
from scripts.connectivity_providers import get_provider


def _check_litellm(base_url: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(f"{to_host_url(base_url)}/health/liveliness", timeout=5.0)
        return resp.status_code == 200, f"{resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    settings = get_settings()
    print("== Bedrock connectivity check ==")

    ok, detail = _check_litellm(settings.litellm_base_url)
    print(f"[{'ok' if ok else 'FAIL'}] LiteLLM reachable at {settings.litellm_base_url} ({detail})")

    try:
        provider = get_provider(settings)
    except ValueError as exc:
        print(f"[FAIL] Bedrock provider: {exc}")
        return 1

    tunnel_ok, tunnel_detail = provider.verify(settings)
    print(f"[{'ok' if tunnel_ok else 'skip'}] Bedrock path ({provider.key}): {tunnel_detail}")
    if not tunnel_ok:
        print("Run `inv tunnel-start` first, then re-run this check.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
