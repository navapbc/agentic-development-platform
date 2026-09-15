"""Run a security scanner in an ephemeral pod from the scanner's own published image.

Scanners are heavy, language-specific tools with their own release cadence. Installing them into
the worker image makes every service carry them and couples the platform's release to theirs.
This runs a scanner from the image its maintainers publish instead, over the same pod transport
the gate engine and project checks already use, and returns the tool's stdout unchanged so the
existing adapters keep parsing the tool's own JSON.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Sequence
from typing import NoReturn
from uuid import uuid4

from adapters.interfaces.security_scanner import ScannerUnavailableError
from adapters.real.security_scanner import CommandResult

from services.orchestration.gates.pod_runner import (
    DEFAULT_BUILDER_IMAGE,
    DEFAULT_NAMESPACE,
    KubectlPodOps,
    PodEngineError,
    PodOps,
    build_pod_manifest,
    pod_name_for,
)

_WORKSPACE_MOUNT = "/workspace"
_OUTPUT_PATH = "/tmp/.scanner-out.json"

# Reserved by scan_script for a failed scanner-image pull. semgrep/error.py defines codes 0-13
# and trivy emits 0-2 unless --exit-code is passed, which the adapters do not pass, so neither
# tool can produce this code and be mistaken for infrastructure failing.
IMAGE_PULL_EXIT = 121


def scanner_pod_name(scanner: str, execution_id: str) -> str:
    return pod_name_for(scanner, execution_id, prefix="scanner")


def remap_paths(args: Sequence[str], cwd: str) -> list[str]:
    """Point the scanner at the workspace mount instead of the worker's copy of the checkout.

    The adapters build their arguments around a path on the worker, which does not exist inside
    the pod.
    """
    if not cwd:
        return list(args)
    return [_WORKSPACE_MOUNT if arg == cwd else arg for arg in args]


def restore_paths(output: str, cwd: str) -> str:
    """Undo the mount substitution so findings carry the same paths as a scan run on the worker.

    Only the tools' location fields are rewritten. A blanket string replacement would also hit
    scanned source text and descriptions the tools embed in their findings, so a repository that
    mentions the mount path would have its own content silently altered.
    """
    if not cwd:
        return output
    try:
        document = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return output
    if not isinstance(document, dict):
        return output
    for key, field in (
        ("results", "path"),
        ("errors", "path"),
        ("Results", "Target"),
    ):
        for finding in document.get(key) or ():
            if isinstance(finding, dict):
                finding[field] = _restore_path(finding.get(field), cwd)
    paths = document.get("paths")
    if isinstance(paths, dict):
        for key in ("scanned", "skipped"):
            entries = paths.get(key)
            if isinstance(entries, list):
                paths[key] = [
                    {**e, "path": _restore_path(e.get("path"), cwd)}
                    if isinstance(e, dict)
                    else _restore_path(e, cwd)
                    for e in entries
                ]
    return json.dumps(document)


def _restore_path(value: object, cwd: str) -> object:
    if not isinstance(value, str):
        return value
    if value == _WORKSPACE_MOUNT:
        return cwd
    prefix = f"{_WORKSPACE_MOUNT}/"
    if value.startswith(prefix):
        return f"{cwd.rstrip('/')}/{value[len(prefix) :]}"
    return value


def scan_script(image: str, args: Sequence[str]) -> str:
    """Build the scanner in its own image and keep the tool's JSON alone on stdout.

    The tool writes to a file that is echoed as the last step, so buildah's own progress output
    cannot corrupt the document the adapter parses. The pod's shell owns the redirect, so the
    file sits outside the workspace mount and the scanner does not report on its own output.

    Pulling the scanner image happens here rather than on the pod's own spec, so it fails as a
    script exit code rather than as a pod event. It exits `IMAGE_PULL_EXIT` so the caller can
    tell a registry that could not be reached from the scanner reporting a vulnerability.
    """
    command = " ".join(shlex.quote(arg) for arg in args)
    return (
        f"ctr=$(buildah from {shlex.quote(image)}) || exit {IMAGE_PULL_EXIT}\n"
        f"buildah run --net host -v {_WORKSPACE_MOUNT}:{_WORKSPACE_MOUNT} "
        f'"$ctr" -- {command} > {_OUTPUT_PATH}\n'
        "rc=$?\n"
        f"cat {_OUTPUT_PATH} 2>/dev/null\n"
        "exit $rc\n"
    )


class PodCommandRunner:
    """A CommandRunner that satisfies the scanner adapters over the pod transport."""

    def __init__(
        self,
        image: str,
        *,
        scanner: str,
        execution_id: str = "",
        ops: PodOps | None = None,
        namespace: str = DEFAULT_NAMESPACE,
        builder_image: str = DEFAULT_BUILDER_IMAGE,
        privileged: bool = False,
    ) -> None:
        self._image = image
        self._scanner = scanner
        self._execution_id = execution_id
        self._ops = ops or KubectlPodOps()
        self._namespace = namespace
        self._builder_image = builder_image
        self._privileged = privileged

    async def __call__(self, args: Sequence[str], cwd: str) -> CommandResult:
        """Run the scanner in a pod, classifying transport failures as unavailability.

        A pod that cannot be created or reached, and a scanner image that cannot be pulled, are
        infrastructure failing rather than the scanner reporting a vulnerability.
        `SecurityScanMediator.scan` turns every other exception into a finding, so without this the
        gate fails closed on a transient registry outage instead of retrying.

        The two failures arrive by different routes. The pod is raised from the builder image, so
        losing it surfaces as `PodEngineError`. The scanner image is pulled by the script running
        inside that pod, so losing it surfaces as `IMAGE_PULL_EXIT` from `exec_capture`.
        """
        pod = scanner_pod_name(self._scanner, self._execution_id or uuid4().hex[:8])
        manifest = build_pod_manifest(
            pod, self._namespace, self._builder_image, self._privileged
        )
        try:
            await self._ops.create(manifest)
            try:
                await self._ops.wait_ready(pod, self._namespace)
                if cwd:
                    await self._ops.copy_in(pod, self._namespace, cwd, _WORKSPACE_MOUNT)
                code, stdout, stderr = await self._ops.exec_capture(
                    pod, self._namespace, scan_script(self._image, remap_paths(args, cwd))
                )
                if code == IMAGE_PULL_EXIT:
                    self._unavailable(f"could not pull {self._image}: {stderr.strip()[:400]}")
                if not stdout.strip() and code == 0:
                    # Both scanners emit a JSON document whenever they run, so a clean exit with
                    # nothing on stdout means the tool never reported. The adapter would parse
                    # that as an empty document and pass it off as a clean scan. A non-zero code
                    # is the scanner's own failure signal and stays with the adapter.
                    self._unavailable(f"{self._image} produced no output: {stderr.strip()[:400]}")
                return CommandResult(
                    returncode=code, stdout=restore_paths(stdout, cwd), stderr=stderr
                )
            finally:
                await self._ops.delete(pod, self._namespace)
        except PodEngineError as exc:
            self._unavailable(str(exc))

    def _unavailable(self, reason: str) -> NoReturn:
        """Report the scanner as unavailable, leaving a trace of why the gate will be retried.

        The retry is silent to an operator otherwise, because the mediator re-runs the scan
        rather than recording the failure as a gate result.
        """
        message = f"{self._scanner} unavailable: {reason}"
        logging.getLogger("gates").warning(message)
        raise ScannerUnavailableError(message)


def pod_command_runner(scanner: str, settings, execution_id: str = "") -> PodCommandRunner:
    images = {"semgrep": settings.semgrep_image, "trivy": settings.trivy_image}
    return PodCommandRunner(
        images[scanner],
        scanner=scanner,
        execution_id=execution_id,
        namespace=settings.gate_engine_namespace,
        builder_image=settings.gate_engine_builder_image,
        privileged=settings.gate_engine_privileged,
    )
