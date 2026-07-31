"""Per-execution agent-server sandbox provisioning on Kubernetes."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_NAME_SANITISE = re.compile(r"[^a-z0-9-]+")


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


@dataclass(frozen=True)
class SandboxHandle:
    name: str
    namespace: str
    base_url: str


def pod_name_for(execution_id: str) -> str:
    slug = _NAME_SANITISE.sub("-", execution_id.lower()).strip("-")
    return f"agent-sandbox-{slug or 'exec'}"[:63].rstrip("-")


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
            "capabilities": {"drop": ["ALL"]},
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
            if phase == "Running" and pod_ip:
                return pod_ip
            if phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"sandbox pod {name} entered terminal phase {phase}")
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

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        handle = SandboxHandle(
            name=pod_name_for(spec.execution_id),
            namespace=spec.namespace,
            base_url=self._base_url,
        )
        self.provisioned.append(handle)
        return handle

    async def teardown(self, handle: SandboxHandle) -> None:
        self.torn_down.append(handle)
