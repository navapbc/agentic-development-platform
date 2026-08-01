"""Per-execution agent-server sandbox provisioning on Kubernetes."""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_NAME_SANITISE = re.compile(r"[^a-z0-9-]+")
_SANDBOX_UID = 10001


@dataclass(frozen=True)
class WorkspaceBinding:
    sub_path: str
    mount_path: str = "/workspace"
    claim_name: str = ""
    host_path: str = ""


@dataclass(frozen=True)
class SandboxSpec:
    execution_id: str
    image: str
    runtime_class: str = ""
    namespace: str = "agent-sandbox"
    server_port: int = 8000
    litellm_base_url: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    workspace: WorkspaceBinding | None = None


@dataclass(frozen=True)
class SandboxHandle:
    name: str
    namespace: str
    base_url: str


def pod_name_for(execution_id: str) -> str:
    slug = _NAME_SANITISE.sub("-", execution_id.lower()).strip("-")
    return f"agent-sandbox-{slug or 'exec'}"[:63].rstrip("-")


def _pod_ready(pod: object) -> bool:
    conditions = getattr(getattr(pod, "status", None), "conditions", None) or []
    return any(
        getattr(c, "type", None) == "Ready" and getattr(c, "status", None) == "True"
        for c in conditions
    )


def _workspace_volume(binding: WorkspaceBinding) -> tuple[dict, dict]:
    mount = {
        "name": "agent-workspace",
        "mountPath": binding.mount_path,
        "subPath": binding.sub_path,
    }
    if binding.claim_name:
        volume = {
            "name": "agent-workspace",
            "persistentVolumeClaim": {"claimName": binding.claim_name},
        }
    elif binding.host_path:
        volume = {
            "name": "agent-workspace",
            "hostPath": {"path": binding.host_path, "type": "Directory"},
        }
    else:
        raise ValueError("workspace binding requires a claim_name or host_path source")
    return volume, mount


def build_pod_manifest(spec: SandboxSpec) -> dict:
    labels = {
        "app": "openhands-sandbox",
        "app.kubernetes.io/part-of": "agentic-dev-platform",
        "agentic-dev-platform/execution-id": spec.execution_id,
        **dict(spec.labels),
    }
    env = [{"name": "LLM_BASE_URL", "value": spec.litellm_base_url}]
    env += [{"name": key, "value": value} for key, value in spec.env.items()]
    container = {
        "name": "agent-server",
        "image": spec.image,
        "ports": [{"containerPort": spec.server_port}],
        "env": env,
        "resources": {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": _SANDBOX_UID,
            "runAsGroup": _SANDBOX_UID,
            "capabilities": {"drop": ["ALL"]},
        },
        "readinessProbe": {
            "exec": {
                "command": [
                    "curl",
                    "-fsS",
                    "-o",
                    "/dev/null",
                    f"http://127.0.0.1:{spec.server_port}/health",
                ]
            },
            "initialDelaySeconds": 2,
            "periodSeconds": 2,
            "timeoutSeconds": 3,
            "failureThreshold": 60,
        },
    }
    spec_body: dict = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
    }
    if spec.runtime_class:
        spec_body["runtimeClassName"] = spec.runtime_class
    if spec.workspace is not None:
        volume, mount = _workspace_volume(spec.workspace)
        container["workingDir"] = spec.workspace.mount_path
        container["volumeMounts"] = [mount]
        env.append({"name": "WORKSPACE_BASE", "value": spec.workspace.mount_path})
        spec_body["volumes"] = [volume]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name_for(spec.execution_id),
            "namespace": spec.namespace,
            "labels": labels,
        },
        "spec": spec_body,
    }


@runtime_checkable
class SandboxProvisioner(Protocol):
    async def provision(self, spec: SandboxSpec) -> SandboxHandle: ...

    async def teardown(self, handle: SandboxHandle) -> None: ...


class KubernetesSandboxProvisioner:
    def __init__(
        self,
        *,
        in_cluster: bool = True,
        ready_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._in_cluster = in_cluster
        self._ready_timeout = ready_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._core = None

    def _client(self):
        if self._core is None:
            from kubernetes import client, config

            if self._in_cluster:
                config.load_incluster_config()
            else:
                config.load_kube_config()
            self._core = client.CoreV1Api()
        return self._core

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        core = self._client()
        manifest = build_pod_manifest(spec)
        name = manifest["metadata"]["name"]
        await asyncio.to_thread(core.create_namespaced_pod, spec.namespace, manifest)
        pod_ip = await self._wait_ready(core, name, spec.namespace)
        base_url = f"http://{pod_ip}:{spec.server_port}"
        return SandboxHandle(name=name, namespace=spec.namespace, base_url=base_url)

    async def _wait_ready(self, core, name: str, namespace: str) -> str:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while True:
            pod = await asyncio.to_thread(core.read_namespaced_pod, name, namespace)
            phase = getattr(pod.status, "phase", None)
            pod_ip = getattr(pod.status, "pod_ip", None)
            if phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"sandbox pod {name} entered terminal phase {phase}")
            if pod_ip and _pod_ready(pod):
                return pod_ip
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"sandbox pod {name} not ready within {self._ready_timeout}s")
            await asyncio.sleep(self._poll_interval)

    async def teardown(self, handle: SandboxHandle) -> None:
        core = self._client()
        from kubernetes.client.exceptions import ApiException

        try:
            await asyncio.to_thread(
                core.delete_namespaced_pod,
                handle.name,
                handle.namespace,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise


class FakeSandboxProvisioner:
    def __init__(self, *, base_url: str = "http://fake-sandbox:8000") -> None:
        self._base_url = base_url
        self.provisioned: list[SandboxHandle] = []
        self.torn_down: list[SandboxHandle] = []
        self.specs: list[SandboxSpec] = []

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        handle = SandboxHandle(
            name=pod_name_for(spec.execution_id),
            namespace=spec.namespace,
            base_url=self._base_url,
        )
        self.specs.append(spec)
        self.provisioned.append(handle)
        return handle

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down.append(handle)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_ready(port: int, path: str = "/health") -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


class PortForwardSandboxProvisioner:
    """Expose an inner provisioner's sandbox over a local kubectl port-forward.

    The Kubernetes provisioner returns a pod-IP base URL that is only routable
    inside the cluster. A host-run driver (a laptop against Docker Desktop
    Kubernetes) reaches the pod through a `kubectl port-forward` and a
    `127.0.0.1` base URL instead.
    """

    def __init__(
        self,
        inner: SandboxProvisioner | None = None,
        *,
        kubectl: str = "kubectl",
        ready_timeout_seconds: float = 120.0,
    ) -> None:
        self._inner = inner or KubernetesSandboxProvisioner(in_cluster=False)
        self._kubectl = kubectl
        self._ready_timeout = ready_timeout_seconds
        self._forwards: dict[str, subprocess.Popen] = {}

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        handle = await self._inner.provision(spec)
        local_port = _free_port()
        proc = await asyncio.to_thread(
            subprocess.Popen,
            [
                self._kubectl,
                "port-forward",
                "-n",
                handle.namespace,
                f"pod/{handle.name}",
                f"{local_port}:{spec.server_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._forwards[handle.name] = proc
        await self._wait_forward(local_port, proc)
        return SandboxHandle(
            name=handle.name,
            namespace=handle.namespace,
            base_url=f"http://127.0.0.1:{local_port}",
        )

    async def _wait_forward(self, port: int, proc: subprocess.Popen) -> None:
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while True:
            if proc.poll() is not None:
                raise RuntimeError("kubectl port-forward exited before becoming ready")
            if await asyncio.to_thread(_http_ready, port):
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"port-forward to 127.0.0.1:{port} not ready in time")
            await asyncio.sleep(0.25)

    async def teardown(self, handle: SandboxHandle) -> None:
        proc = self._forwards.pop(handle.name, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
        await self._inner.teardown(handle)
