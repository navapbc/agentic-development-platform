"""Run a project's declared checks in one isolated, ephemeral build-and-run pod.

The checks run in a fresh pod the coding agent never sees, against a clean checkout of the branch at
the commit under review. The command runs in the declared base image, or in an image built from a
declared Dockerfile, with the checkout bind-mounted. The pod captures the command's exit code and
combined output and is torn down. Only the captured output is returned, so the caller can gate on
the exit code and feed the log into the repair loop. This reuses the external-gate pod transport, so
it is identical in Docker Desktop Kubernetes and on EKS and needs no registry.
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass
from typing import Any

from services.orchestration.gates.pod_runner import (
    PodEngineError,
    PodOps,
    _tail,
    build_pod_manifest,
)

DEFAULT_BUILDER_IMAGE = "quay.io/buildah/stable:v1.43.2"
DEFAULT_NAMESPACE = "agent-sandbox"

_WORKSPACE_MOUNT = "/workspace"
_CHECKS_TAG = "project-checks"
_HOST_GATEWAY_ALIAS = "host.docker.internal"
_OWNERSHIP_FLAGS = (
    "-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=*"
)


class ChecksError(PodEngineError):
    """The checks pod could not build or start the declared command."""


@dataclass(frozen=True)
class ChecksSpec:
    repo: str
    execution_id: str
    checkout_dir: str
    command: str
    image: str = ""
    build: str = ""
    working_dir: str = "/workspace"


@dataclass(frozen=True)
class ChecksResult:
    exit_code: int
    output: str


def checks_pod_name(repo: str, execution_id: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9-]+", "-", f"{repo}-{execution_id}".lower()).strip("-")
    return f"project-checks-{slug or 'run'}"[:63].rstrip("-")


def _checks_script(spec: ChecksSpec) -> str:
    b64 = base64.b64encode(spec.command.encode()).decode()
    if spec.build:
        dockerfile = f"{_WORKSPACE_MOUNT}/{spec.build.lstrip('/')}"
        acquire = (
            f"buildah bud -f {shlex.quote(dockerfile)} -t {_CHECKS_TAG} "
            f"{shlex.quote(_WORKSPACE_MOUNT)} >&2 && img={_CHECKS_TAG}"
        )
    else:
        acquire = f"img={shlex.quote(spec.image)}"
    inner = f"cd {shlex.quote(spec.working_dir)} && echo {b64} | base64 -d | sh"
    return (
        "set -e\n"
        f"{acquire}\n"
        'hostflag=""\n'
        f"gw=$(getent hosts {_HOST_GATEWAY_ALIAS} 2>/dev/null | awk '{{print $1}}' | head -1)\n"
        f'if [ -n "$gw" ]; then hostflag="--add-host {_HOST_GATEWAY_ALIAS}:$gw"; fi\n'
        'ctr=$(buildah from $hostflag "$img")\n'
        "set +e\n"
        f"buildah run --net host -v {_WORKSPACE_MOUNT}:{_WORKSPACE_MOUNT} "
        f"{_OWNERSHIP_FLAGS} "
        f'"$ctr" -- sh -lc {shlex.quote(inner)} 2>&1\n'
        "rc=$?\n"
        "exit $rc\n"
    )


class ChecksRunner:
    def __init__(
        self,
        ops: PodOps,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        builder_image: str = DEFAULT_BUILDER_IMAGE,
    ) -> None:
        self._ops = ops
        self._namespace = namespace
        self._builder_image = builder_image

    async def run_checks(self, spec: ChecksSpec) -> ChecksResult:
        pod = checks_pod_name(spec.repo, spec.execution_id)
        manifest = build_pod_manifest(pod, self._namespace, self._builder_image)
        await self._ops.create(manifest)
        try:
            await self._ops.wait_ready(pod, self._namespace)
            await self._ops.copy_in(pod, self._namespace, spec.checkout_dir, _WORKSPACE_MOUNT)
            code, stdout, stderr = await self._ops.exec_capture(
                pod, self._namespace, _checks_script(spec)
            )
            output = stdout if not stderr else f"{stdout}\n{stderr}" if stdout else stderr
            return ChecksResult(exit_code=code, output=output.strip())
        finally:
            await self._ops.delete(pod, self._namespace)


def _summary(result: ChecksResult) -> str:
    return _tail(result.output)


def describe_outcome(result: ChecksResult) -> dict[str, Any]:
    return {"exit_code": result.exit_code, "summary": _summary(result)}
