"""Run an external gate as an ephemeral build-and-run pod, no registry required.

The gate tool executes inside one throwaway pod started from a public buildah base image.
The pod builds the tool's own Dockerfile into its local image store (or uses a published image
when one is named), then runs it against the checkout under review with the tool source and the
checkout bind-mounted, writing its findings JSON to a file the runner reads back. The built image
never leaves the pod, so nothing is pushed to a registry, and the same path runs in Docker Desktop
Kubernetes and on EKS. Model calls route through the in-cluster LiteLLM gateway.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_NAME_SANITISE = re.compile(r"[^a-z0-9-]+")

_ENGINE_MOUNT = "/engine"
_WORKSPACE_MOUNT = "/workspace"
_OUTPUT_PATH = "/workspace/.gate-engine-out.json"
_BUILD_TAG = "gate-tool"
_HOST_GATEWAY_ALIAS = "host.docker.internal"

DEFAULT_BUILDER_IMAGE = (
    "quay.io/buildah/stable:v1.43.5"
    "@sha256:7c2b605262748ace2c5c57c5550942d0adad57aec82ede3070927722306c0ffb"
)
DEFAULT_NAMESPACE = "agent-sandbox"


class PodEngineError(RuntimeError):
    """The build-and-run pod failed to produce a parseable findings document."""


def _tail(text: str, limit: int = 2000) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return "...(truncated)... " + stripped[-limit:]


@dataclass(frozen=True)
class PodEngineSpec:
    name: str
    execution_id: str
    checkout_dir: str
    entrypoint: str
    base_ref: str
    source_path: str = ""
    image: str = ""
    tls_verify: bool = True
    env: Mapping[str, str] = field(default_factory=dict)


def pod_name_for(name: str, execution_id: str, prefix: str = "gate-engine") -> str:
    slug = _NAME_SANITISE.sub("-", f"{name}-{execution_id}".lower()).strip("-")
    return f"{prefix}-{slug or 'run'}"[:63].rstrip("-")


DEFAULT_POD_RESOURCES: dict[str, dict[str, str]] = {
    "requests": {"cpu": "500m", "memory": "1Gi"},
    "limits": {"cpu": "2", "memory": "4Gi"},
}

# A project's test suite runs under the same wall-clock assertions its authors wrote on a developer
# machine. Helpers such as testing-library's waitFor budget a fixed 1000ms that no jest flag
# overrides, so a CPU-starved pod fails tests that are not defective. Two CPUs reproduces those
# failures and six clears them.
CHECKS_POD_RESOURCES: dict[str, dict[str, str]] = {
    "requests": {"cpu": "2", "memory": "2Gi"},
    "limits": {"cpu": "6", "memory": "8Gi"},
}


def build_pod_manifest(
    pod: str,
    namespace: str,
    builder_image: str,
    privileged: bool = False,
    resources: dict[str, dict[str, str]] | None = None,
) -> dict:
    # Unpacking an image layer remounts the container root MS_PRIVATE|MS_REC, which some clusters
    # (k3s, Colima) deny to an unprivileged pod even with CAP_SYS_ADMIN and seccomp unconfined.
    security_context: dict[str, Any] = {"runAsUser": 0}
    if privileged:
        security_context["privileged"] = True
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod,
            "namespace": namespace,
            "labels": {
                "app": "gate-engine",
                "app.kubernetes.io/part-of": "agentic-dev-platform",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "builder",
                    "image": builder_image,
                    "command": ["sleep", "infinity"],
                    "env": [
                        {"name": "STORAGE_DRIVER", "value": "vfs"},
                        {"name": "BUILDAH_ISOLATION", "value": "chroot"},
                    ],
                    "securityContext": security_context,
                    "resources": deepcopy(resources or DEFAULT_POD_RESOURCES),
                }
            ],
        },
    }


def _run_script(spec: PodEngineSpec) -> str:
    """The build-and-run script executed inside the pod.

    Builds the tool image from the copied source when no published image is named, mounts the tool
    source and the checkout into the run, executes the entrypoint against the base ref, and writes
    the findings JSON to a file the runner reads back. Exit status is the entrypoint's, and the
    engines treat 0 and 1 as success (findings or none) and any other code as a failure.
    """
    env_flags = " ".join(
        f"-e {shlex.quote(f'{key}={value}')}" for key, value in sorted(spec.env.items())
    )
    ownership_flags = (
        "-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=*"
    )
    entrypoint = f"{_ENGINE_MOUNT}/{spec.entrypoint.lstrip('/')}"
    invoke = (
        f"bash {shlex.quote(entrypoint)} "
        f"--against {shlex.quote(spec.base_ref)} "
        f"--json-out {shlex.quote(_OUTPUT_PATH)}"
    )
    # The agent's change is the uncommitted working tree in the copied checkout, so commit it as a
    # snapshot before the review. The engine reviews `git diff <base_ref> HEAD`, which sees the
    # change only once it is a commit on top of the base ref. Runs as root because the checkout is
    # copied in root-owned while the engine image runs as a non-root user.
    snapshot = (
        f"cd {_WORKSPACE_MOUNT} && git add -A && "
        "git -c user.email=gate@agentic.local -c user.name=gate "
        "commit -q -m gate-review-snapshot 2>/dev/null || true"
    )
    if spec.image:
        acquire = f"img={shlex.quote(spec.image)}"
    else:
        acquire = (
            f"buildah bud -t {_BUILD_TAG} {shlex.quote(_ENGINE_MOUNT)} >&2 && img={_BUILD_TAG}"
        )
    # TLS verification is only relevant to the registry pull in image mode. Disabling it lets the
    # gate pod pull from a plain-HTTP local registry. The build path never touches a registry.
    tls_flag = "--tls-verify=false " if spec.image and not spec.tls_verify else ""
    return (
        "set -e\n"
        f"{acquire}\n"
        'hostflag=""\n'
        f"gw=$(getent hosts {_HOST_GATEWAY_ALIAS} 2>/dev/null | awk '{{print $1}}' | head -1)\n"
        f'if [ -n "$gw" ]; then hostflag="--add-host {_HOST_GATEWAY_ALIAS}:$gw"; fi\n'
        f'ctr=$(buildah from {tls_flag}$hostflag "$img")\n'
        # The checkout is copied in as root, but the engine image runs as a non-root user, so make
        # the mount directory writable for the engine to create its --json-out file.
        f"chmod 0777 {shlex.quote(_WORKSPACE_MOUNT)}\n"
        "set +e\n"
        f"buildah run --user root -v {_WORKSPACE_MOUNT}:{_WORKSPACE_MOUNT} {ownership_flags} "
        f'"$ctr" -- sh -c {shlex.quote(snapshot)} >&2\n'
        f"buildah run --net host -v {_ENGINE_MOUNT}:{_ENGINE_MOUNT} "
        f"-v {_WORKSPACE_MOUNT}:{_WORKSPACE_MOUNT} {ownership_flags} {env_flags} "
        f'"$ctr" -- {invoke} >&2\n'
        "rc=$?\n"
        f'if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then echo "engine exited $rc" >&2; exit $rc; fi\n'
        f"cat {shlex.quote(_OUTPUT_PATH)}\n"
    )


@runtime_checkable
class PodOps(Protocol):
    async def create(self, manifest: dict) -> None: ...

    async def wait_ready(self, pod: str, namespace: str) -> None: ...

    async def copy_in(self, pod: str, namespace: str, source_dir: str, dest_dir: str) -> None: ...

    async def copy_out(
        self, pod: str, namespace: str, source_dir: str, dest_dir: str, manifest_path: str
    ) -> None: ...

    async def exec_capture(self, pod: str, namespace: str, script: str) -> tuple[int, str, str]: ...

    async def delete(self, pod: str, namespace: str) -> None: ...


class PodEngineRunner:
    def __init__(
        self,
        ops: PodOps,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        builder_image: str = DEFAULT_BUILDER_IMAGE,
        privileged: bool = False,
    ) -> None:
        self._ops = ops
        self._namespace = namespace
        self._builder_image = builder_image
        self._privileged = privileged

    async def run_engine(self, spec: PodEngineSpec) -> dict[str, Any]:
        pod = pod_name_for(spec.name, spec.execution_id)
        manifest = build_pod_manifest(pod, self._namespace, self._builder_image, self._privileged)
        await self._ops.create(manifest)
        try:
            await self._ops.wait_ready(pod, self._namespace)
            if spec.source_path:
                await self._ops.copy_in(pod, self._namespace, spec.source_path, _ENGINE_MOUNT)
            await self._ops.copy_in(pod, self._namespace, spec.checkout_dir, _WORKSPACE_MOUNT)
            code, stdout, stderr = await self._ops.exec_capture(
                pod, self._namespace, _run_script(spec)
            )
            if code != 0:
                raise PodEngineError(
                    f"gate {spec.name!r} engine pod exited {code}: {_tail(stderr)}"
                )
            try:
                return json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise PodEngineError(
                    f"gate {spec.name!r} engine produced no parseable JSON: {exc}"
                ) from exc
        finally:
            await self._ops.delete(pod, self._namespace)


class KubectlPodOps:
    """PodOps over kubectl, mirroring the sandbox provisioner's exec-and-tar transport."""

    def __init__(self, *, kubectl: str = "kubectl", ready_timeout_seconds: float = 180.0) -> None:
        self._kubectl = kubectl
        self._ready_timeout = ready_timeout_seconds

    async def create(self, manifest: dict) -> None:
        import asyncio

        await asyncio.to_thread(self._create_sync, manifest)

    def _create_sync(self, manifest: dict) -> None:
        from services import process

        proc = process.run(
            [self._kubectl, "apply", "-f", "-"],
            timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
            input=json.dumps(manifest),
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise PodEngineError(f"pod create failed: {proc.stderr.strip()}")

    async def wait_ready(self, pod: str, namespace: str) -> None:
        import asyncio

        await asyncio.to_thread(self._wait_ready_sync, pod, namespace)

    def _wait_ready_sync(self, pod: str, namespace: str) -> None:
        from services import process

        proc = process.run(
            [
                self._kubectl,
                "wait",
                "-n",
                namespace,
                f"pod/{pod}",
                "--for=condition=Ready",
                f"--timeout={int(self._ready_timeout)}s",
            ],
            timeout=self._ready_timeout + process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        if proc.returncode:
            raise PodEngineError(f"pod {pod} not ready: {proc.stderr.strip()}")

    async def copy_in(self, pod: str, namespace: str, source_dir: str, dest_dir: str) -> None:
        import asyncio

        await asyncio.to_thread(self._copy_in_sync, pod, namespace, source_dir, dest_dir)

    def _copy_in_sync(self, pod: str, namespace: str, source_dir: str, dest_dir: str) -> None:
        from services import process

        quoted = shlex.quote(dest_dir)
        script = f"mkdir -p {quoted} && tar xmf - --no-same-owner -C {quoted}"
        tar = process.popen(["tar", "-C", source_dir, "-cf", "-", "."], stdout=process.PIPE)
        exec_proc = process.popen(
            [self._kubectl, "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script],
            stdin=tar.stdout,
            stderr=process.PIPE,
        )
        if tar.stdout is not None:
            tar.stdout.close()
        _, stderr = exec_proc.communicate()
        tar.wait()
        if exec_proc.returncode:
            raise PodEngineError((stderr or b"").decode().strip() or "copy_in failed")

    async def copy_out(
        self, pod: str, namespace: str, source_dir: str, dest_dir: str, manifest_path: str
    ) -> None:
        import asyncio

        await asyncio.to_thread(
            self._copy_out_sync, pod, namespace, source_dir, dest_dir, manifest_path
        )

    def _copy_out_sync(
        self, pod: str, namespace: str, source_dir: str, dest_dir: str, manifest_path: str
    ) -> None:
        from services import process

        script = (
            f"tar cf - --ignore-failed-read -C {shlex.quote(source_dir)} "
            f"-T {shlex.quote(manifest_path)}"
        )
        exec_proc = process.popen(
            [self._kubectl, "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script],
            stdout=process.PIPE,
            stderr=process.PIPE,
        )
        untar = process.popen(
            ["tar", "xmf", "-", "--no-same-owner", "-C", dest_dir], stdin=exec_proc.stdout
        )
        if exec_proc.stdout is not None:
            exec_proc.stdout.close()
        untar.wait()
        _, stderr = exec_proc.communicate()
        if untar.returncode or exec_proc.returncode:
            raise PodEngineError((stderr or b"").decode().strip() or "copy_out failed")

    async def exec_capture(self, pod: str, namespace: str, script: str) -> tuple[int, str, str]:
        import asyncio

        return await asyncio.to_thread(self._exec_capture_sync, pod, namespace, script)

    def _exec_capture_sync(self, pod: str, namespace: str, script: str) -> tuple[int, str, str]:
        from services import process

        proc = process.run(
            [self._kubectl, "exec", "-i", "-n", namespace, pod, "--", "sh", "-c", script],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    async def delete(self, pod: str, namespace: str) -> None:
        import asyncio

        await asyncio.to_thread(self._delete_sync, pod, namespace)

    def _delete_sync(self, pod: str, namespace: str) -> None:
        from services import process

        process.run(
            [
                self._kubectl,
                "delete",
                "pod",
                pod,
                "-n",
                namespace,
                "--grace-period=0",
                "--ignore-not-found",
            ],
            timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
