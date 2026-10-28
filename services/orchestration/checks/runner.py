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

from services.orchestration.gates.pod_runner import (
    PodOps,
    build_pod_manifest,
)

DEFAULT_BUILDER_IMAGE = (
    "quay.io/buildah/stable:v1.43.5"
    "@sha256:7c2b605262748ace2c5c57c5550942d0adad57aec82ede3070927722306c0ffb"
)
DEFAULT_NAMESPACE = "agent-sandbox"

_WORKSPACE_MOUNT = "/workspace"
_CHECKS_TAG = "project-checks"
_HOST_GATEWAY_ALIAS = "host.docker.internal"
_OWNERSHIP_FLAGS = (
    "-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=*"
)
_PATH_SEPARATOR = "\n"


@dataclass(frozen=True)
class ChecksSpec:
    repo: str
    execution_id: str
    checkout_dir: str
    command: str
    image: str = ""
    build: str = ""
    working_dir: str = "/workspace"
    fix_command: str = ""
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecksResult:
    exit_code: int
    output: str


@dataclass(frozen=True)
class ChecksFixResult:
    """Outcome of running a project's declared fix command and re-running its checks."""

    verified: bool
    exit_code: int
    output: str
    changed_files: tuple[str, ...] = ()


def checks_pod_name(repo: str, execution_id: str) -> str:
    """Name a checks pod so concurrent runs, and a repo's fix and check pods, never collide.

    The discriminator is a digest appended after truncation because a long repo slug would
    otherwise consume the whole 63-character limit and leave the execution id out of the name.
    """
    import hashlib
    import re

    digest = hashlib.sha256(f"{repo}-{execution_id}".encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-")
    prefix = f"project-checks-{slug or 'run'}"[:52].rstrip("-")
    return f"{prefix}-{digest}"


def _changed_paths_statement(spec: ChecksSpec) -> str:
    """Hand the declared command the paths the agent changed, so it can scope its own checks.

    The paths are newline-separated inside one shell-quoted value, so a path containing a space
    survives. A command that needs an argument vector converts them with ``tr '\\n' '\\0' |
    xargs -0`` rather than relying on word splitting. An empty set exports nothing, which lets a
    command distinguish "nothing changed" from "scoping unavailable" and fall back to its full run.
    """
    if not spec.changed_paths:
        return ""
    return f"export CHANGED_PATHS={shlex.quote(_PATH_SEPARATOR.join(spec.changed_paths))}"


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
    changed = _changed_paths_statement(spec)
    if changed:
        inner = f"{changed}\n{inner}"
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


_MARKER = f"{_WORKSPACE_MOUNT}/.gatefix.marker"
_MANIFEST = f"{_WORKSPACE_MOUNT}/.gatefix.list"
_FILES_BEGIN = "GATEFIX_FILES_BEGIN"
_FILES_END = "GATEFIX_FILES_END"
_VERIFY_RC = "GATEFIX_VERIFY_RC="
_FIX_FAILED = "fix command exited "


def _fix_inner_script(spec: ChecksSpec) -> str:
    """Run the project's fix command, list what it changed, then re-run the checks to verify.

    Runs inside the project's own image so the fix uses the project's toolchain, not the
    platform's. The marker and manifest live under the bind-mounted workspace so the pod can tar
    the changed files back out, and are pruned from the listing so they never escape the pod.

    The script's exit status is the verify command's, because a fix is only applied when the
    project's own checks pass afterwards. A fix command that fails on its own would otherwise
    leave no trace, so it announces its status into the output the summary keeps.
    """
    wd = shlex.quote(spec.working_dir)
    changed = _changed_paths_statement(spec)
    lines = [
        f"cd {wd} || exit 1",
        f"touch {_MARKER}",
        spec.fix_command,
        "fix_rc=$?",
        f'[ "$fix_rc" -eq 0 ] || echo "{_FIX_FAILED}$fix_rc"',
        f"cd {_WORKSPACE_MOUNT} || exit 1",
        (
            "find . \\( -name node_modules -o -name .git -o -name .venv "
            "-o -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache "
            "-o -name .mypy_cache \\) -prune -o "
            f"-type f -newer {_MARKER} -print 2>/dev/null "
            f"| grep -v '/\\.gatefix\\.' | grep -v '^\\./\\.gatefix\\.' > {_MANIFEST}"
        ),
        f"echo {_FILES_BEGIN}",
        f"cat {_MANIFEST}",
        f"echo {_FILES_END}",
        f"cd {wd} || exit 1",
        spec.command,
        "verify_rc=$?",
        f'echo "{_VERIFY_RC}$verify_rc"',
        "exit $verify_rc",
    ]
    if changed:
        lines.insert(0, changed)
    return "\n".join(lines)


def _fix_script(spec: ChecksSpec) -> str:
    b64 = base64.b64encode(_fix_inner_script(spec).encode()).decode()
    if spec.build:
        dockerfile = f"{_WORKSPACE_MOUNT}/{spec.build.lstrip('/')}"
        acquire = (
            f"buildah bud -f {shlex.quote(dockerfile)} -t {_CHECKS_TAG} "
            f"{shlex.quote(_WORKSPACE_MOUNT)} >&2 && img={_CHECKS_TAG}"
        )
    else:
        acquire = f"img={shlex.quote(spec.image)}"
    inner = f"echo {b64} | base64 -d | sh"
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


def parse_fix_output(text: str) -> tuple[tuple[str, ...], str]:
    """Split the changed-file manifest out of the fix pod's combined output."""
    files: list[str] = []
    kept: list[str] = []
    collecting = False
    for line in text.splitlines():
        if line.strip() == _FILES_BEGIN:
            collecting = True
            continue
        if line.strip() == _FILES_END:
            collecting = False
            continue
        if collecting:
            entry = line.strip().removeprefix("./")
            if entry:
                files.append(entry)
            continue
        if line.startswith(_VERIFY_RC):
            continue
        kept.append(line)
    return tuple(files), "\n".join(kept).strip()


class ChecksRunner:
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

    async def run_checks(self, spec: ChecksSpec) -> ChecksResult:
        pod = checks_pod_name(spec.repo, spec.execution_id)
        manifest = build_pod_manifest(pod, self._namespace, self._builder_image, self._privileged)
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

    async def run_fix(self, spec: ChecksSpec, staging_dir: str) -> ChecksFixResult:
        """Apply the project's declared fix command, verify it, and stage what it changed.

        The repair only counts when re-running the project's own checks passes, so a fixer that
        edits files without resolving the failure is reported as unverified rather than applied.
        """
        if not spec.fix_command:
            return ChecksFixResult(verified=False, exit_code=1, output="no fix_command declared")
        pod = checks_pod_name(spec.repo, f"{spec.execution_id}-fix")
        manifest = build_pod_manifest(pod, self._namespace, self._builder_image, self._privileged)
        await self._ops.create(manifest)
        try:
            await self._ops.wait_ready(pod, self._namespace)
            await self._ops.copy_in(pod, self._namespace, spec.checkout_dir, _WORKSPACE_MOUNT)
            code, stdout, stderr = await self._ops.exec_capture(
                pod, self._namespace, _fix_script(spec)
            )
            combined = stdout if not stderr else f"{stdout}\n{stderr}" if stdout else stderr
            changed, output = parse_fix_output(combined)
            if code == 0 and changed:
                await self._ops.copy_out(
                    pod, self._namespace, _WORKSPACE_MOUNT, staging_dir, _MANIFEST
                )
            return ChecksFixResult(
                verified=code == 0 and bool(changed),
                exit_code=code,
                output=output,
                changed_files=changed,
            )
        finally:
            await self._ops.delete(pod, self._namespace)
