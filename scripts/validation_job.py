"""The deployment-validate Job, rendered into both overlays and run from its CronJob template.

The eks-deployer image runs this module with the Python standard library and the scripts it
copies, so it imports nothing beyond `services.process`.

`run_job` creates a Job from the `deployment-validate` CronJob's template, which runs the
validation with the scope's arguments or another platform module given as its entry point, waits
for it, and returns whether it passed with its container's exit code and its log.
`inv deployment-validate` runs it in the current kube context, and `inv k8s-reload`,
`inv k8s-deploy`, and the eks-deployer `deploy` command run `report_after_rollout`, which prints
the failures and never fails the deploy.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services import process

CRONJOB = "deployment-validate"
CONTAINER = "deployment-validate"
PLATFORM_NAMESPACE = "agentic-dev-platform"
VALIDATE_ENTRY = "scripts.deployment_validate"
POLL_SECONDS = 5.0
WAIT_SECONDS = 960.0
LOG = "[deployment-validate]"
ALERT_PLATFORM = "--alert-platform"

Runner = Callable[..., process.CompletedProcess[Any]]


@dataclass(frozen=True)
class JobResult:
    passed: bool
    exit_code: int | None
    report: str


def _containers(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]


def validation_cronjob(documents: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (d for d in documents if d.get("kind") == "CronJob" and d["metadata"]["name"] == CRONJOB),
        None,
    )


def set_validation_image(documents: Iterable[dict[str, Any]], image: str) -> None:
    cronjob = validation_cronjob(documents)
    if cronjob is not None:
        for container in _containers(cronjob):
            container["image"] = image


def set_validation_tenants(documents: Iterable[dict[str, Any]], tenants: Sequence[str]) -> None:
    """Name the tenants `--all-tenants` validates, which the EKS settings do not carry."""
    cronjob = validation_cronjob(documents)
    if cronjob is not None:
        for container in _containers(cronjob):
            container.setdefault("env", []).append({"name": "TENANTS", "value": ",".join(tenants)})


def scope_args(*, platform: bool = False, all_tenants: bool = False, tenant: str = "") -> list[str]:
    """Return the scope argument of exactly one of platform, all tenants, or one tenant."""
    chosen = [flag for flag, on in (("--platform", platform), ("--all-tenants", all_tenants)) if on]
    if tenant:
        chosen.append(f"--tenant={tenant}")
    if len(chosen) != 1:
        raise ValueError("choose exactly one of --platform, --all-tenants, or --tenant")
    return chosen


def job_from_cronjob(
    cronjob: Mapping[str, Any], args: Sequence[str], name: str, *, entry: str = VALIDATE_ENTRY
) -> dict[str, Any]:
    """Return a Job of the CronJob's template that runs the module ``entry`` with ``args``.

    The Job is owned by the CronJob, as `kubectl create job --from` makes it, so deleting the
    CronJob deletes it.
    """
    template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
    for container in template["spec"]["template"]["spec"]["containers"]:
        if container["name"] == CONTAINER:
            container["args"] = ["python", "-m", entry, *args]
    metadata = cronjob["metadata"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": metadata["namespace"],
            "labels": dict(template.get("metadata", {}).get("labels") or {}),
            "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "name": metadata["name"],
                    "uid": metadata["uid"],
                    "controller": False,
                }
            ],
        },
        "spec": template["spec"],
    }


def _kubectl(
    run: Runner, kubectl: Sequence[str], namespace: str, *args: str, stdin: str | None = None
) -> str:
    result = run(
        [*kubectl, "--namespace", namespace, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {args[0]} failed: {(result.stderr or '').strip()}")
    return result.stdout or ""


def _outcome(job: Mapping[str, Any]) -> str:
    status = job.get("status") or {}
    if status.get("succeeded"):
        return "passed"
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Failed" and condition.get("status") == "True":
            return condition.get("message") or condition.get("reason") or "failed"
    if status.get("failed"):
        return "failed"
    return ""


def _exit_code(run: Runner, kubectl: Sequence[str], namespace: str, job: str) -> int | None:
    try:
        pods = json.loads(
            _kubectl(run, kubectl, namespace, "get", "pods", "-l", f"job-name={job}", "-o", "json")
        )
    except RuntimeError:
        return None
    for pod in pods.get("items", []):
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            terminated = (status.get("state") or {}).get("terminated") or {}
            if status.get("name") == CONTAINER and "exitCode" in terminated:
                return terminated["exitCode"]
    return None


def run_job(
    kubectl: Sequence[str],
    namespace: str,
    args: Sequence[str],
    *,
    entry: str = VALIDATE_ENTRY,
    run: Runner = process.run,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: float = WAIT_SECONDS,
) -> JobResult:
    """Run the Job, whose exit code is None when its container left no terminated state."""
    cronjob = json.loads(_kubectl(run, kubectl, namespace, "get", "cronjob", CRONJOB, "-o", "json"))
    name = f"{entry.rpartition('.')[2].replace('_', '-')}-{int(time.time())}"
    manifest = json.dumps(job_from_cronjob(cronjob, args, name, entry=entry))
    _kubectl(run, kubectl, namespace, "create", "-f", "-", stdin=manifest)
    deadline = clock() + wait_seconds
    outcome = ""
    while not outcome:
        job = json.loads(_kubectl(run, kubectl, namespace, "get", "job", name, "-o", "json"))
        outcome = _outcome(job)
        if outcome:
            break
        if clock() >= deadline:
            return JobResult(
                False, None, f"the Job {name} did not finish within {int(wait_seconds)} seconds"
            )
        sleep(POLL_SECONDS)
    try:
        report = _kubectl(run, kubectl, namespace, "logs", f"job/{name}", "-c", CONTAINER).strip()
    except RuntimeError:
        report = ""
    passed = outcome == "passed"
    if not passed and not report:
        report = f"the Job {name} printed no report and stopped with {outcome}"
    return JobResult(passed, 0 if passed else _exit_code(run, kubectl, namespace, name), report)


def failure_lines(report: str) -> list[str]:
    """Keep each section heading and each failing check with its cause, fix, and confirmation."""
    kept: list[str] = []
    in_failure = False
    for line in report.splitlines():
        if line.startswith("FAIL "):
            in_failure = True
            kept.append(line)
        elif line.startswith("  ") and in_failure:
            kept.append(line)
        else:
            in_failure = False
            if line.startswith("Deployment validation"):
                kept.append(line)
    return kept


def report_after_rollout(
    kubectl: Sequence[str], namespace: str = PLATFORM_NAMESPACE, *, run: Runner = process.run
) -> bool:
    """Validate every tenant after a rollout and print the failures, never failing the deploy.

    The failing platform checks also reach the administrator destination.
    """
    print(f"{LOG} validating the platform and every tenant", flush=True)
    try:
        args = [*scope_args(all_tenants=True), ALERT_PLATFORM]
        result = run_job(kubectl, namespace, args, run=run)
    except (RuntimeError, ValueError, OSError, process.SubprocessError) as exc:
        print(f"{LOG} the validation did not run, because {exc}", flush=True)
        return False
    if result.passed:
        print(f"{LOG} every check passed", flush=True)
        return True
    for line in failure_lines(result.report) or result.report.splitlines():
        print(f"{LOG} {line}", flush=True)
    print(f"{LOG} the deploy continues. Run inv deployment-validate --all-tenants after the fix.")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--platform", action="store_true")
    scope.add_argument("--all-tenants", action="store_true")
    scope.add_argument("--tenant", default="")
    parser.add_argument("--post", action="store_true")
    parser.add_argument("--context", default="")
    parser.add_argument("--namespace", default=PLATFORM_NAMESPACE)
    args = parser.parse_args(argv)
    kubectl = ["kubectl", *(["--context", args.context] if args.context else [])]
    validation = scope_args(
        platform=args.platform, all_tenants=args.all_tenants, tenant=args.tenant
    )
    if args.post:
        validation.append("--post")
    try:
        result = run_job(kubectl, args.namespace, validation)
    except RuntimeError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1
    print(result.report)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
