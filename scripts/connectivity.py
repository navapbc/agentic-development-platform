"""Validate the Bedrock path and local service reachability."""

from __future__ import annotations

import socket
import ssl

import httpx

from scripts.config import get_settings, to_host_url


def _check_litellm(base_url: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(f"{to_host_url(base_url)}/health/liveliness", timeout=5.0)
        return resp.status_code == 200, f"{resp.status_code}"
    except Exception as exc:
        return False, str(exc)


def _check_bedrock_tunnel(host: str, local_port: int) -> tuple[bool, str]:
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection(("127.0.0.1", local_port), timeout=5.0) as sock,
            context.wrap_socket(sock, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
        return bool(cert), f"TLS to {host} via 127.0.0.1:{local_port} verified"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    settings = get_settings()
    print("== Bedrock connectivity check ==")

    ok, detail = _check_litellm(settings.litellm_base_url)
    print(f"[{'ok' if ok else 'FAIL'}] LiteLLM reachable at {settings.litellm_base_url} ({detail})")

    tunnel_ok, tunnel_detail = _check_bedrock_tunnel(
        settings.bedrock_runtime_host, settings.bedrock_tunnel_local_port
    )
    print(f"[{'ok' if tunnel_ok else 'skip'}] Bedrock tunnel: {tunnel_detail}")
    if not tunnel_ok:
        print("Run `inv tunnel-start` first, then re-run this check.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
