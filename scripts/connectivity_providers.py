"""Pluggable Bedrock connectivity providers (R7).

Platform code depends only on a reachability contract: a network path to the
bedrock-runtime endpoint, an IAM principal with ``bedrock:InvokeModel``, and an
injected endpoint URL plus region and credential chain. Each provider satisfies that
contract with a different network path (SSM jump-host, SSH bastion, in-VPC direct, or
public endpoint). No single provider is baked into the platform code paths.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.config import Settings


@runtime_checkable
class BedrockConnectivity(Protocol):
    key: str

    def start(self, settings: Settings) -> int:
        """Establish (or explain) the network path to bedrock-runtime."""
        ...

    def verify(self, settings: Settings) -> tuple[bool, str]:
        """Return (ok, detail) for the reachability of the path this provider owns."""
        ...

    def summary(self, settings: Settings) -> str:
        """One-line human description of the configured path."""
        ...


def _tls_check(server_hostname: str, connect_host: str, port: int) -> tuple[bool, str]:
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((connect_host, port), timeout=5.0) as sock,
            context.wrap_socket(sock, server_hostname=server_hostname) as tls,
        ):
            cert = tls.getpeercert()
        via = f"{connect_host}:{port}"
        where = via if connect_host == server_hostname else f"{server_hostname} via {via}"
        return bool(cert), f"TLS to {where} verified"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _resolve_connect_script(configured: str) -> Path | None:
    candidate = configured or os.environ.get("JUMPHOST_CONNECT_SCRIPT", "")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    return path if path.is_file() else None


class JumphostConnectivity:
    """SSM port-forward through a config-driven jump-host."""

    key = "jumphost"

    def summary(self, settings: Settings) -> str:
        return (
            f"jump-host {settings.jumphost_name or '<unset>'} (env "
            f"{settings.jumphost_env or '<unset>'}) forwarding "
            f"{settings.bedrock_tunnel_local_port} -> {settings.bedrock_runtime_host}:443"
        )

    def start(self, settings: Settings) -> int:
        script = _resolve_connect_script(settings.jumphost_connect_script)
        if not settings.jumphost_name or not settings.jumphost_env:
            print(
                "[tunnel] FAIL: JUMPHOST_NAME and JUMPHOST_ENV must be set for the "
                "jumphost provider.",
                file=sys.stderr,
            )
            return 2
        if script is None:
            print("[tunnel] FAIL: connect script not found.", file=sys.stderr)
            print(
                "[tunnel] Set JUMPHOST_CONNECT_SCRIPT to the path of the connect script.",
                file=sys.stderr,
            )
            return 2

        forwarding = f"{settings.bedrock_tunnel_local_port}:{settings.bedrock_runtime_host}:443"
        cmd = [
            str(script),
            "-n",
            settings.jumphost_name,
            "-e",
            settings.jumphost_env,
            "-p",
            forwarding,
        ]
        env = {
            **os.environ,
            "AWS_PROFILE": settings.aws_profile,
            "AWS_REGION": settings.aws_region,
        }
        print(f"[tunnel] exec: {' '.join(cmd)}")
        os.execvpe(cmd[0], cmd, env)

    def verify(self, settings: Settings) -> tuple[bool, str]:
        return _tls_check(
            settings.bedrock_runtime_host, "127.0.0.1", settings.bedrock_tunnel_local_port
        )


class SshBastionConnectivity:
    """SSH local port-forward through a generic bastion host."""

    key = "ssh-bastion"

    def summary(self, settings: Settings) -> str:
        return (
            f"ssh bastion {settings.ssh_bastion_target or '<unset>'} forwarding "
            f"{settings.bedrock_tunnel_local_port} -> {settings.bedrock_runtime_host}:443"
        )

    def start(self, settings: Settings) -> int:
        if not settings.ssh_bastion_target:
            print(
                "[tunnel] FAIL: SSH_BASTION_TARGET must be set for the ssh-bastion provider.",
                file=sys.stderr,
            )
            return 2
        forwarding = f"{settings.bedrock_tunnel_local_port}:{settings.bedrock_runtime_host}:443"
        cmd = ["ssh", "-N", "-L", forwarding, settings.ssh_bastion_target]
        print(f"[tunnel] exec: {' '.join(cmd)}")
        os.execvpe(cmd[0], cmd, {**os.environ})

    def verify(self, settings: Settings) -> tuple[bool, str]:
        return _tls_check(
            settings.bedrock_runtime_host, "127.0.0.1", settings.bedrock_tunnel_local_port
        )


class DirectConnectivity:
    """No tunnel: the caller already resolves bedrock-runtime inside the VPC."""

    key = "direct"

    def summary(self, settings: Settings) -> str:
        target = settings.bedrock_endpoint_url or f"https://{settings.bedrock_runtime_host}"
        return f"direct in-VPC path to {target}"

    def start(self, settings: Settings) -> int:
        print("[tunnel] direct provider: no tunnel needed; assuming in-VPC resolution.")
        print(f"[tunnel]   target: {self.summary(settings)}")
        return 0

    def verify(self, settings: Settings) -> tuple[bool, str]:
        return _tls_check(settings.bedrock_runtime_host, settings.bedrock_runtime_host, 443)


class PublicConnectivity:
    """Public bedrock-runtime endpoint (synthetic-data dev only, off by default)."""

    key = "public"

    def summary(self, settings: Settings) -> str:
        return f"public endpoint {settings.bedrock_runtime_host}:443"

    def start(self, settings: Settings) -> int:
        print("[tunnel] public provider: no tunnel; reaching the public Bedrock endpoint.")
        print(f"[tunnel]   target: {self.summary(settings)}")
        return 0

    def verify(self, settings: Settings) -> tuple[bool, str]:
        return _tls_check(settings.bedrock_runtime_host, settings.bedrock_runtime_host, 443)


_PROVIDERS: dict[str, BedrockConnectivity] = {
    p.key: p
    for p in (
        JumphostConnectivity(),
        SshBastionConnectivity(),
        DirectConnectivity(),
        PublicConnectivity(),
    )
}


def get_provider(settings: Settings) -> BedrockConnectivity:
    key = settings.bedrock_connectivity_provider
    provider = _PROVIDERS.get(key)
    if provider is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown bedrock_connectivity_provider {key!r}; known: {known}")
    if key == "public" and not settings.allow_public_bedrock_endpoint:
        raise ValueError(
            "public bedrock provider requires allow_public_bedrock_endpoint=true "
            "(synthetic-data dev only, explicit sign-off)"
        )
    return provider
