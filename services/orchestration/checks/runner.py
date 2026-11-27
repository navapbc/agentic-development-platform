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
import contextlib
import shlex
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.orchestration.gates import image_admission
from services.orchestration.gates import registry_auth as registry_auth_script
from services.orchestration.gates.dockerfile_images import base_images, dockerfile_for
from services.orchestration.gates.pod_runner import (
    CHECKS_POD_RESOURCES,
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
    lock_command: str = ""


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


@dataclass(frozen=True)
class ChecksLockResult:
    """Outcome of regenerating a project's lockfiles, with the files the lock command wrote."""

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


def _tls_flag(spec: ChecksSpec, tls_verify: bool) -> str:
    # A built image is local to the pod, so only a pulled image reaches a registry.
    return "--tls-verify=false " if spec.image and not tls_verify else ""


def _encoded(script: str) -> str:
    return f"echo {base64.b64encode(script.encode()).decode()} | base64 -d | sh"


def _container_script(
    spec: ChecksSpec,
    inner: str,
    registry: registry_auth_script.RegistrySetup,
    tls_verify: bool,
) -> str:
    """Acquire the project's image, built or pulled, and run `inner` in it on the workspace."""
    if spec.build:
        dockerfile = f"{_WORKSPACE_MOUNT}/{spec.build.lstrip('/')}"
        acquire = (
            f"buildah bud {registry.build_flags()}-f {shlex.quote(dockerfile)} -t {_CHECKS_TAG} "
            f"{shlex.quote(_WORKSPACE_MOUNT)} >&2 && img={_CHECKS_TAG}"
        )
    else:
        acquire = f"img={shlex.quote(spec.image)}"
    return (
        "set -e\n"
        f"{registry.before_pull()}"
        f"{acquire}\n"
        'hostflag=""\n'
        f"gw=$(getent hosts {_HOST_GATEWAY_ALIAS} 2>/dev/null | awk '{{print $1}}' | head -1)\n"
        f'if [ -n "$gw" ]; then hostflag="--add-host {_HOST_GATEWAY_ALIAS}:$gw"; fi\n'
        f'ctr=$(buildah from $hostflag {_tls_flag(spec, tls_verify)}"$img")\n'
        f"{registry.after_pull()}"
        "set +e\n"
        f"buildah run --net host -v {_WORKSPACE_MOUNT}:{_WORKSPACE_MOUNT} "
        f"{_OWNERSHIP_FLAGS} {registry.run_flags()}"
        f'"$ctr" -- sh -lc {shlex.quote(inner)} 2>&1\n'
        "rc=$?\n"
        "exit $rc\n"
    )


def _checks_script(
    spec: ChecksSpec,
    registry: registry_auth_script.RegistrySetup = registry_auth_script.NO_REGISTRY,
    tls_verify: bool = True,
) -> str:
    inner = f"cd {shlex.quote(spec.working_dir)} && {_encoded(spec.command)}"
    changed = _changed_paths_statement(spec)
    if changed:
        inner = f"{changed}\n{inner}"
    return _container_script(spec, inner, registry, tls_verify)


_MARKER = f"{_WORKSPACE_MOUNT}/.gatefix.marker"
_MANIFEST = f"{_WORKSPACE_MOUNT}/.gatefix.list"
_FILES_BEGIN = "GATEFIX_FILES_BEGIN"
_FILES_END = "GATEFIX_FILES_END"
_VERIFY_RC = "GATEFIX_VERIFY_RC="
_FIX_FAILED = "fix command exited "


_LIST_CHANGED_FILES = (
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
)


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
        *_LIST_CHANGED_FILES,
        f"cd {wd} || exit 1",
        spec.command,
        "verify_rc=$?",
        f'echo "{_VERIFY_RC}$verify_rc"',
        "exit $verify_rc",
    ]
    if changed:
        lines.insert(0, changed)
    return "\n".join(lines)


def _lock_inner_script(spec: ChecksSpec, directories: Sequence[str]) -> str:
    """Run the lock command in each directory and list the files it wrote, and nothing else.

    No checks command runs, so the lockfile comes out whether or not the project's tests would
    pass, and the script exits with the first failing lock command's status.
    """
    lines = [f"touch {_MARKER}", "lock_rc=0"]
    for directory in directories:
        path = f"{_WORKSPACE_MOUNT}/{directory}".rstrip("/")
        lines.append(
            f"cd {shlex.quote(path)} && {spec.lock_command} || "
            '{ rc=$?; [ "$lock_rc" -ne 0 ] || lock_rc=$rc; }'
        )
    lines += [*_LIST_CHANGED_FILES, "exit $lock_rc"]
    return "\n".join(lines)


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


def _build_images(spec: ChecksSpec) -> list[str]:
    """Return the images the build Dockerfile pulls, which the pod admits like its own images."""
    if not spec.build:
        return []
    return base_images(dockerfile_for(Path(spec.checkout_dir), spec.build))


class ChecksRunner:
    def __init__(
        self,
        ops: PodOps,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        builder_image: str = DEFAULT_BUILDER_IMAGE,
        privileged: bool = False,
        node_pool: str = "",
        tls_verify: bool = True,
    ) -> None:
        self._ops = ops
        self._tls_verify = tls_verify
        self._namespace = namespace
        self._builder_image = builder_image
        self._privileged = privileged
        self._node_pool = node_pool

    @contextlib.asynccontextmanager
    async def _pod(
        self, spec: ChecksSpec, suffix: str = ""
    ) -> AsyncIterator[tuple[str, registry_auth_script.RegistrySetup]]:
        """A checks pod holding the checkout, with the registry setup for the project's image."""
        pod = checks_pod_name(spec.repo, f"{spec.execution_id}{suffix}")
        manifest = build_pod_manifest(
            pod,
            self._namespace,
            self._builder_image,
            self._privileged,
            CHECKS_POD_RESOURCES,
            node_pool=self._node_pool,
            service_account=image_admission.pod_service_account(),
        )
        images = [spec.image, self._builder_image, *_build_images(spec)]
        await self._ops.create(manifest)
        try:
            await self._ops.wait_ready(pod, self._namespace)
            await self._ops.copy_in(pod, self._namespace, spec.checkout_dir, _WORKSPACE_MOUNT)
            yield pod, await registry_auth_script.install(self._ops, pod, self._namespace, images)
        finally:
            await self._ops.delete(pod, self._namespace)

    async def _exec(self, pod: str, script: str) -> tuple[int, str]:
        code, stdout, stderr = await self._ops.exec_capture(pod, self._namespace, script)
        return code, stdout if not stderr else f"{stdout}\n{stderr}" if stdout else stderr

    async def run_checks(self, spec: ChecksSpec) -> ChecksResult:
        async with self._pod(spec) as (pod, registry):
            code, output = await self._exec(pod, _checks_script(spec, registry, self._tls_verify))
        return ChecksResult(exit_code=code, output=output.strip())

    async def run_fix(self, spec: ChecksSpec, staging_dir: str) -> ChecksFixResult:
        """Apply the project's declared fix command, verify it, and stage what it changed.

        The repair only counts when re-running the project's own checks passes, so a fixer that
        edits files without resolving the failure is reported as unverified rather than applied.
        """
        if not spec.fix_command:
            return ChecksFixResult(verified=False, exit_code=1, output="no fix_command declared")
        code, changed, output = await self._run_staged(
            spec, "-fix", _fix_inner_script(spec), staging_dir
        )
        return ChecksFixResult(
            verified=code == 0 and bool(changed),
            exit_code=code,
            output=output,
            changed_files=changed,
        )

    async def run_lock(
        self, spec: ChecksSpec, directories: Sequence[str], staging_dir: str
    ) -> ChecksLockResult:
        """Regenerate the lockfiles of `directories`, relative to the checkout, and stage them."""
        code, changed, output = await self._run_staged(
            spec, "-lock", _lock_inner_script(spec, directories), staging_dir
        )
        return ChecksLockResult(exit_code=code, output=output, changed_files=changed)

    async def _run_staged(
        self, spec: ChecksSpec, suffix: str, script: str, staging_dir: str
    ) -> tuple[int, tuple[str, ...], str]:
        """Run `script` in the project's image and stage the files it changed when it exits 0."""
        async with self._pod(spec, suffix) as (pod, registry):
            code, combined = await self._exec(
                pod, _container_script(spec, _encoded(script), registry, self._tls_verify)
            )
            changed, output = parse_fix_output(combined)
            if code == 0 and changed:
                await self._ops.copy_out(
                    pod, self._namespace, _WORKSPACE_MOUNT, staging_dir, _MANIFEST
                )
        return code, changed, output


def checks_runner(settings) -> ChecksRunner:
    """The checks runner the settings configure, over the kubectl pod transport."""
    from services.orchestration.gates import pod_runner

    return ChecksRunner(
        pod_runner.KubectlPodOps(),
        namespace=settings.gate_engine_namespace,
        builder_image=settings.gate_engine_builder_image,
        privileged=settings.gate_engine_privileged,
        node_pool=settings.sandbox_node_pool,
        tls_verify=settings.gate_registry_tls_verify,
    )
