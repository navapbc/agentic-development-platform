"""Pluggable Bedrock connectivity providers.

Platform code depends only on a reachability contract: a network path to the
bedrock-runtime endpoint, an IAM principal with ``bedrock:InvokeModel``, and an
injected endpoint URL plus region and credential chain. Each provider satisfies that
contract with a different network path (SSM jump-host, SSH bastion, in-VPC direct, or
public endpoint). No single provider is baked into the platform code paths.
"""

from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.config import Settings


@runtime_checkable
class BedrockConnectivity(Protocol):
    key: str

    def start(self, settings: Settings, *, detach: bool = False) -> int:
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


_TUNNEL_PIDFILE = Path(tempfile.gettempdir()) / "agentic-dev-platform-bedrock-tunnel.pid"
_TUNNEL_LOGFILE = Path(tempfile.gettempdir()) / "agentic-dev-platform-bedrock-tunnel.log"


def _read_tunnel_pid() -> int | None:
    try:
        return int(_TUNNEL_PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _tunnel_running() -> bool:
    pid = _read_tunnel_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tunnel_log_hint() -> str:
    try:
        text = _TUNNEL_LOGFILE.read_text(errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    keywords = (
        "expired",
        "error",
        "denied",
        "refused",
        "unauthorized",
        "forbidden",
        "not authorized",
        "invalid",
        "timed out",
        "timeout",
    )
    hits = [line for line in lines if any(k in line.lower() for k in keywords)]
    return hits[-1] if hits else lines[-1]


def _print_tunnel_failure_hint() -> None:
    hint = _tunnel_log_hint()
    if not hint:
        return
    print(f"[tunnel]   cause: {hint}", file=sys.stderr)
    lowered = hint.lower()
    if "expired" in lowered or "token" in lowered:
        print(
            "[tunnel]   hint: refresh your AWS credentials (e.g. aws sso login) and retry.",
            file=sys.stderr,
        )


def _spawn_detached(
    cmd: list[str],
    env: dict[str, str],
    *,
    label: str,
    ready: Callable[[], tuple[bool, str]] | None = None,
    ready_timeout: float = 45.0,
) -> int:
    if _tunnel_running():
        print(
            f"[tunnel] a background tunnel is already running (pid {_read_tunnel_pid()}); "
            "run `inv tunnel-stop` first.",
            file=sys.stderr,
        )
        return 2
    print(f"[tunnel] spawn: {' '.join(cmd)}")
    with _TUNNEL_LOGFILE.open("w") as log:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _TUNNEL_PIDFILE.write_text(str(proc.pid))
    time.sleep(1.0)
    if proc.poll() is not None:
        _TUNNEL_PIDFILE.unlink(missing_ok=True)
        print(
            f"[tunnel] FAIL: {label} exited immediately (rc {proc.returncode}); "
            f"see {_TUNNEL_LOGFILE}",
            file=sys.stderr,
        )
        _print_tunnel_failure_hint()
        return 1
    if ready is not None:
        print(
            f"[tunnel] waiting for {label} to accept connections (up to {int(ready_timeout)}s) ..."
        )
        deadline = time.monotonic() + ready_timeout
        last_detail = "no attempt yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _TUNNEL_PIDFILE.unlink(missing_ok=True)
                print(
                    f"[tunnel] FAIL: {label} exited before it was ready "
                    f"(rc {proc.returncode}); see {_TUNNEL_LOGFILE}",
                    file=sys.stderr,
                )
                _print_tunnel_failure_hint()
                return 1
            ok, last_detail = ready()
            if ok:
                print(f"[tunnel] {label} ready: {last_detail}")
                break
            time.sleep(1.0)
        else:
            print(
                f"[tunnel] WARN: {label} still not ready after {int(ready_timeout)}s "
                f"(last: {last_detail}); leaving it running.",
                file=sys.stderr,
            )
            print(f"[tunnel]   logs: {_TUNNEL_LOGFILE}")
            print("[tunnel]   retry check: inv connectivity-check")
            print("[tunnel]   stop: inv tunnel-stop")
            return 1
    print(f"[tunnel] {label} running in background (pid {proc.pid})")
    print(f"[tunnel]   logs: {_TUNNEL_LOGFILE}")
    if ready is not None:
        print("[tunnel]   verified: safe to run inv connectivity-check")
    print("[tunnel]   stop: inv tunnel-stop")
    return 0


def stop_tunnel() -> int:
    pid = _read_tunnel_pid()
    if pid is None:
        print("[tunnel] no background tunnel to stop.")
        return 0
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        _TUNNEL_PIDFILE.unlink(missing_ok=True)
        print(f"[tunnel] no live process for pid {pid}; cleared stale pidfile.")
        return 0
    except OSError as exc:
        print(f"[tunnel] FAIL: could not stop pid {pid}: {exc}", file=sys.stderr)
        return 1
    for _ in range(50):
        if not _tunnel_running():
            break
        time.sleep(0.1)
    _TUNNEL_PIDFILE.unlink(missing_ok=True)
    print(f"[tunnel] stopped background tunnel (pid {pid}).")
    return 0


def status_tunnel() -> int:
    pid = _read_tunnel_pid()
    if pid is None:
        print("[tunnel] no background tunnel recorded (pidfile absent).")
        return 1
    if _tunnel_running():
        print(f"[tunnel] running (pid {pid}).")
        print(f"[tunnel]   logs: {_TUNNEL_LOGFILE}")
        print("[tunnel]   verify path: inv connectivity-check")
        return 0
    print(f"[tunnel] not running; stale pidfile records pid {pid}.")
    print("[tunnel]   clean up: inv tunnel-stop")
    return 1


class JumphostConnectivity:
    """SSM port-forward through a config-driven jump-host."""

    key = "jumphost"

    def summary(self, settings: Settings) -> str:
        return (
            f"jump-host {settings.jumphost_name or '<unset>'} (env "
            f"{settings.jumphost_env or '<unset>'}) forwarding "
            f"{settings.bedrock_tunnel_local_port} -> {settings.bedrock_runtime_host}:443"
        )

    def start(self, settings: Settings, *, detach: bool = False) -> int:
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
        if detach:
            return _spawn_detached(
                cmd, env, label="jump-host tunnel", ready=lambda: self.verify(settings)
            )
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

    def start(self, settings: Settings, *, detach: bool = False) -> int:
        if not settings.ssh_bastion_target:
            print(
                "[tunnel] FAIL: SSH_BASTION_TARGET must be set for the ssh-bastion provider.",
                file=sys.stderr,
            )
            return 2
        forwarding = f"{settings.bedrock_tunnel_local_port}:{settings.bedrock_runtime_host}:443"
        cmd = ["ssh", "-N", "-L", forwarding, settings.ssh_bastion_target]
        env = {**os.environ}
        if detach:
            return _spawn_detached(
                cmd, env, label="ssh bastion tunnel", ready=lambda: self.verify(settings)
            )
        print(f"[tunnel] exec: {' '.join(cmd)}")
        os.execvpe(cmd[0], cmd, env)

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

    def start(self, settings: Settings, *, detach: bool = False) -> int:
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

    def start(self, settings: Settings, *, detach: bool = False) -> int:
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
