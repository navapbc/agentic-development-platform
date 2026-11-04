"""Deploy the Temporal server: apply the schema, roll the server, then register the namespace.

Terraform registers the task definitions and leaves the server service at its current task
definition and count. This runs the admin-tools schema task to completion, moves the service to
the task definition Terraform last registered at the configured count, waits for it to settle,
and runs the namespace task. A schema task that does not exit 0 stops the deploy before the
server changes, so a server never starts against a schema older than it needs.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from services import process

from scripts.temporal_certs import COMPOSITION_ROOT

TASK_WAIT = {"Delay": 10, "MaxAttempts": 90}
SERVICE_WAIT = {"Delay": 15, "MaxAttempts": 80}


class DeployError(Exception):
    """A deploy step failed, and the server was left as it was before that step."""


def run_admin_tools(ecs: Any, deploy: dict[str, Any], action: str) -> None:
    """Run the admin-tools task with `action` and wait for it to exit 0."""
    started = ecs.run_task(
        cluster=deploy["cluster"],
        taskDefinition=deploy["admin_tools_task_definition"],
        capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": deploy["admin_tools_subnets"],
                "securityGroups": deploy["admin_tools_security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": "admin-tools", "command": [action]}]},
        startedBy="temporal-deploy",
    )
    if started.get("failures"):
        raise DeployError(f"the {action} task did not start: {started['failures']}")
    task_arn = started["tasks"][0]["taskArn"]
    print(f"[temporal-deploy] running the {action} task {task_arn.rsplit('/', 1)[-1]}")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=deploy["cluster"], tasks=[task_arn], WaiterConfig=TASK_WAIT
    )
    task = ecs.describe_tasks(cluster=deploy["cluster"], tasks=[task_arn])["tasks"][0]
    container = next(c for c in task["containers"] if c["name"] == "admin-tools")
    exit_code = container.get("exitCode")
    if exit_code != 0:
        raise DeployError(
            f"the {action} task exited with {exit_code} "
            f"({task.get('stoppedReason', 'no reason')}). "
            "Its output is in the admin-tools log group."
        )


def roll_server(ecs: Any, deploy: dict[str, Any]) -> None:
    ecs.update_service(
        cluster=deploy["cluster"],
        service=deploy["server_service"],
        taskDefinition=deploy["server_task_definition"],
        desiredCount=deploy["server_count"],
    )
    print(
        f"[temporal-deploy] moved {deploy['server_service']} to "
        f"{deploy['server_task_definition'].rsplit('/', 1)[-1]} at {deploy['server_count']} task(s)"
    )
    ecs.get_waiter("services_stable").wait(
        cluster=deploy["cluster"], services=[deploy["server_service"]], WaiterConfig=SERVICE_WAIT
    )


def deploy(ecs: Any, settings: dict[str, Any]) -> None:
    if not settings:
        raise DeployError(
            "The control plane has no task to deploy. Set create_temporal_control_plane, "
            "temporal_server_image, and temporal_admin_tools_image, and apply private-bedrock."
        )
    run_admin_tools(ecs, settings, "schema")
    roll_server(ecs, settings)
    run_admin_tools(ecs, settings, "namespace")


def _terraform_deploy(env: dict[str, str]) -> dict[str, Any]:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "temporal_deploy"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise DeployError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout) or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="")
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)
    import boto3

    ecs = boto3.Session(region_name=args.region or settings.aws_region).client("ecs")
    try:
        deploy(ecs, _terraform_deploy(dict(os.environ)))
    except DeployError as exc:
        print(f"[temporal-deploy] {exc}")
        return 1
    print("[temporal-deploy] the server runs the current task definition and the namespace exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
