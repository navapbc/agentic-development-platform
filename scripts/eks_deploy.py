"""Run the eks-deployer task, which deploys the EKS workloads from inside the VPC.

The workstation needs no kubectl, helm, or route to the private Kubernetes API. This reads the
`eks` output of the composition root, checks which secrets the overlay mounts hold a current value,
and starts the eks-deployer one-shot task on the platform ECS cluster with the `deploy` command and
the secrets to leave out. A pod whose SecretProviderClass names a secret without a value never
starts, so a workload runs without an external secret its environment has not set yet. The task's
log is printed once it stops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from services import process

from scripts.eks_render import secret_key
from scripts.temporal_certs import COMPOSITION_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = REPO_ROOT / "deploy" / "kubernetes" / "aws" / "secret-provider-classes.yaml"
TASK = "eks-deployer"
TASK_WAIT = {"Delay": 10, "MaxAttempts": 180}
LOG = "[eks-deploy]"


class DeployError(Exception):
    """The eks-deployer task could not run or did not succeed."""


def mounted_secret_keys(providers: Path = PROVIDERS) -> list[str]:
    keys: set[str] = set()
    for document in yaml.safe_load_all(providers.read_text(encoding="utf-8")):
        if document:
            for entry in yaml.safe_load(document["spec"]["parameters"]["objects"]):
                keys.add(secret_key(entry["objectName"]))
    return sorted(keys)


def unset_secrets(keys: list[str], arns: dict[str, str], secretsmanager: Any) -> list[str]:
    """Return the keys whose secret has no AWSCURRENT version, which no pod could mount."""
    unset = []
    for key in keys:
        arn = arns.get(key)
        if not arn:
            raise DeployError(f"the eks output names no secret for {key}")
        stages = secretsmanager.describe_secret(SecretId=arn).get("VersionIdsToStages") or {}
        if not any("AWSCURRENT" in labels for labels in stages.values()):
            unset.append(key)
    return unset


def run_deployer(ecs: Any, logs: Any, deployer: dict[str, Any], command: list[str]) -> int:
    task = deployer["tasks"][TASK]
    started = ecs.run_task(
        cluster=deployer["cluster"],
        taskDefinition=task["task_definition"],
        capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": deployer["task_subnets"],
                "securityGroups": task["security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": task["container"], "command": command}]},
        startedBy="eks-deploy",
    )
    if started.get("failures"):
        raise DeployError(f"the eks-deployer task did not start: {started['failures']}")
    task_arn = started["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    print(f"{LOG} running {' '.join(command)} as task {task_id}")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=deployer["cluster"], tasks=[task_arn], WaiterConfig=TASK_WAIT
    )
    stopped = ecs.describe_tasks(cluster=deployer["cluster"], tasks=[task_arn])["tasks"][0]
    container = next(c for c in stopped["containers"] if c["name"] == task["container"])
    print_log(logs, deployer["log_group"], f"{task['container']}/{task['container']}/{task_id}")
    exit_code = container.get("exitCode")
    if exit_code != 0:
        raise DeployError(
            f"the eks-deployer task exited with {exit_code} "
            f"({stopped.get('stoppedReason', 'no reason')})"
        )
    return exit_code


def print_log(logs: Any, group: str, stream: str) -> None:
    try:
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=group, logStreamNames=[stream]):
            for event in page["events"]:
                print(event["message"])
    except logs.exceptions.ResourceNotFoundException:
        print(f"{LOG} the task wrote no log to {group}")


def _terraform_eks(env: dict[str, str]) -> dict[str, Any] | None:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "eks"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise DeployError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "null")


def deploy(session: Any, eks: dict[str, Any] | None, command: list[str]) -> int:
    if not eks or not eks.get("deployer"):
        print(f"{LOG} the composition root has no EKS cluster or eks-deployer image, skipping")
        return 0
    if command[0] == "deploy":
        unset = unset_secrets(
            mounted_secret_keys(), eks["secret_arns"], session.client("secretsmanager")
        )
        for key in unset:
            print(f"{LOG} {key} has no value, so the pods that mount it start without it")
        command = command + [part for key in unset for part in ("--omit-secret", key)]
    return run_deployer(session.client("ecs"), session.client("logs"), eks["deployer"], command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="")
    parser.add_argument(
        "command",
        nargs="*",
        default=["deploy"],
        help="deploy, check-policies, or restart DEPLOYMENT...",
    )
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)
    import boto3

    session = boto3.Session(region_name=args.region or settings.aws_region)
    try:
        return deploy(session, _terraform_eks(dict(os.environ)), args.command or ["deploy"])
    except DeployError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
