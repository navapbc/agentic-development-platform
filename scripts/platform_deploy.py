"""Deploy the platform services: migrate, roll the services, register the schedules, prune.

Terraform registers the task definitions and leaves each service at its current task definition
and count. This runs db-migrate as the migrator role and stops unless it exits 0, so no service
starts against a schema older than its build. With Langfuse, it then moves ClickHouse to its task
definition, waits for it to settle, and runs the Langfuse ClickHouse migrations with certificate
verification. It then moves each other service to the task definition Terraform last registered
and waits for every service to settle.

A Temporal worker runs in a blue and a green slot. A new build starts in the idle slot and makes
itself the current version, so new executions start on it, while the old slot keeps serving the
executions pinned to the old build. The old slot is scaled to zero once no running execution is
pinned to its build, by this deploy or a later one. A task definition revision of the same build
replaces the active slot's tasks in place. Last, the deploy registers the reap and intake
schedules and deletes the Worker Deployment versions no execution is pinned to.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from services import process

from scripts.pinned_executions import PINNED_EXIT_CODE
from scripts.publish_platform_images import build_id_of
from scripts.temporal_certs import COMPOSITION_ROOT

TASK_WAIT = {"Delay": 10, "MaxAttempts": 120}
SERVICE_WAIT = {"Delay": 15, "MaxAttempts": 80}
PRUNE_COMMAND = ["python", "-m", "scripts.prune_worker_versions"]
DATA_SERVICES = ("clickhouse",)
LOG = "[platform-deploy]"


class DeployError(Exception):
    """A deploy step failed, and the services were left as they were before that step."""


def run_task(
    ecs: Any,
    deploy: dict[str, Any],
    name: str,
    command: list[str] | None = None,
    accept: tuple[int, ...] = (0,),
) -> int:
    """Run the one-shot task `name`, optionally with another command, and return its exit code.

    An exit code outside `accept` raises DeployError.
    """
    task = deploy["tasks"][name]
    override: dict[str, Any] = {"name": task["container"]}
    if command:
        override["command"] = command
    started = ecs.run_task(
        cluster=deploy["cluster"],
        taskDefinition=task["task_definition"],
        capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": deploy["task_subnets"],
                "securityGroups": task["security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [override]},
        startedBy="platform-deploy",
    )
    label = " ".join(command) if command else name
    if started.get("failures"):
        raise DeployError(f"{label} did not start: {started['failures']}")
    task_arn = started["tasks"][0]["taskArn"]
    print(f"{LOG} running {label} as task {task_arn.rsplit('/', 1)[-1]}")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=deploy["cluster"], tasks=[task_arn], WaiterConfig=TASK_WAIT
    )
    stopped = ecs.describe_tasks(cluster=deploy["cluster"], tasks=[task_arn])["tasks"][0]
    container = next(c for c in stopped["containers"] if c["name"] == task["container"])
    exit_code = container.get("exitCode")
    if exit_code not in accept:
        raise DeployError(
            f"{label} exited with {exit_code} ({stopped.get('stoppedReason', 'no reason')}). "
            f"Its output is in the {name} log group."
        )
    return exit_code


def _move(ecs: Any, cluster: str, service: str, task_definition: str, count: int) -> None:
    ecs.update_service(
        cluster=cluster,
        service=service,
        taskDefinition=task_definition,
        desiredCount=count,
        forceNewDeployment=True,
    )
    print(f"{LOG} moved {service} to {task_definition.rsplit('/', 1)[-1]} at {count} task(s)")


def _build_of(ecs: Any, task_definition: str) -> str:
    described = ecs.describe_task_definition(taskDefinition=task_definition)["taskDefinition"]
    images = [
        c["image"] for c in described["containerDefinitions"] if c["name"] != "volume-permissions"
    ]
    return build_id_of(images[0].split("@")[0].rsplit(":", 1)[1])


def _slot_states(ecs: Any, cluster: str, slots: dict[str, str]) -> dict[str, dict[str, Any]]:
    described = ecs.describe_services(cluster=cluster, services=list(slots.values()))["services"]
    by_name = {service["serviceName"]: service for service in described}
    return {
        slot: {
            "service": name,
            "count": by_name[name]["desiredCount"],
            "task_definition": by_name[name]["taskDefinition"],
        }
        for slot, name in slots.items()
    }


def _pinned(ecs: Any, deploy: dict[str, Any], build: str, checked: dict[str, int]) -> bool:
    """Report whether running executions are pinned to `build`, once per build per deploy."""
    if build not in checked:
        checked[build] = run_task(
            ecs,
            deploy,
            "schedule-bootstrap",
            ["python", "-m", "scripts.pinned_executions", build],
            accept=(0, PINNED_EXIT_CODE),
        )
    return checked[build] == PINNED_EXIT_CODE


def plan_versioned(
    ecs: Any, deploy: dict[str, Any], name: str, checked: dict[str, int]
) -> tuple[str, str | None]:
    """Choose the slot of `name` that takes the new task definition and the slot left to drain.

    A slot running the target build takes the new revision in place. Otherwise the idle slot
    takes the new build and the running slot, if any, is left to drain. With both slots running
    older builds, a slot whose build no running execution is pinned to takes the new build.
    """
    worker = deploy["versioned_services"][name]
    states = _slot_states(ecs, deploy["cluster"], worker["slots"])
    target_build = _build_of(ecs, worker["task_definition"])
    running = [slot for slot, state in states.items() if state["count"] > 0]
    for slot in running:
        if _build_of(ecs, states[slot]["task_definition"]) == target_build:
            return slot, next((other for other in running if other != slot), None)
    if len(running) == len(states):
        free = [
            slot
            for slot in running
            if not _pinned(ecs, deploy, _build_of(ecs, states[slot]["task_definition"]), checked)
        ]
        if not free:
            raise DeployError(
                f"both {name} slots still run older builds with pinned executions, so the new "
                "build has no slot. Let those executions finish, or resume the parked ones, and "
                "deploy again."
            )
        return free[0], next(slot for slot in running if slot != free[0])
    idle = next(slot for slot in states if slot not in running)
    return idle, running[0] if running else None


def drain(ecs: Any, deploy: dict[str, Any], name: str, slot: str, checked: dict[str, int]) -> None:
    """Scale the old slot of `name` to zero when no running execution is pinned to its build.

    `checked` holds the pinned-execution report of each build already checked in this deploy.
    """
    cluster = deploy["cluster"]
    state = _slot_states(ecs, cluster, deploy["versioned_services"][name]["slots"])[slot]
    build = _build_of(ecs, state["task_definition"])
    if _pinned(ecs, deploy, build, checked):
        print(
            f"{LOG} {state['service']} keeps build {build}, which running executions are pinned "
            "to. A later deploy scales it to zero once they finish."
        )
        return
    ecs.update_service(cluster=cluster, service=state["service"], desiredCount=0)
    print(f"{LOG} scaled {state['service']} to zero, as no execution is pinned to build {build}")


def _single_services(deploy: dict[str, Any]) -> list[dict[str, Any]]:
    langfuse = deploy.get("langfuse_services", {})
    return list(deploy["services"].values()) + [
        service for name, service in langfuse.items() if name not in DATA_SERVICES
    ]


def roll_data_services(ecs: Any, deploy: dict[str, Any]) -> None:
    """Move ClickHouse to its task definition, wait for it, and migrate its Langfuse schema."""
    data = [
        service
        for name, service in deploy.get("langfuse_services", {}).items()
        if name in DATA_SERVICES
    ]
    if not data:
        return
    for service in data:
        _move(
            ecs, deploy["cluster"], service["service"], service["task_definition"], service["count"]
        )
    ecs.get_waiter("services_stable").wait(
        cluster=deploy["cluster"],
        services=[service["service"] for service in data],
        WaiterConfig=SERVICE_WAIT,
    )
    run_task(ecs, deploy, "clickhouse-migrate")


def plan_slots(
    ecs: Any, deploy: dict[str, Any], checked: dict[str, int]
) -> dict[str, tuple[str, str | None]]:
    return {
        name: plan_versioned(ecs, deploy, name, checked)
        for name in deploy.get("versioned_services", {})
    }


def roll_services(
    ecs: Any, deploy: dict[str, Any], plans: dict[str, tuple[str, str | None]]
) -> dict[str, str]:
    """Move every service to its current task definition, returning the slots left to drain."""
    versioned = deploy.get("versioned_services", {})
    cluster = deploy["cluster"]
    single = _single_services(deploy)
    for service in single:
        _move(ecs, cluster, service["service"], service["task_definition"], service["count"])
    for name, (slot, _) in plans.items():
        worker = versioned[name]
        _move(ecs, cluster, worker["slots"][slot], worker["task_definition"], worker["count"])
    services = [service["service"] for service in single] + [
        service for worker in versioned.values() for service in worker["slots"].values()
    ]
    ecs.get_waiter("services_stable").wait(
        cluster=cluster, services=services, WaiterConfig=SERVICE_WAIT
    )
    return {name: old for name, (_, old) in plans.items() if old is not None}


def deploy(ecs: Any, settings: dict[str, Any]) -> None:
    if not settings:
        raise DeployError(
            "The container services have nothing to deploy. Set create_container_services, "
            "container_platform_image, container_repo_intelligence_image, and the internal "
            "load balancer certificate, and apply private-bedrock."
        )
    run_task(ecs, settings, "db-migrate")
    checked: dict[str, int] = {}
    plans = plan_slots(ecs, settings, checked)
    roll_data_services(ecs, settings)
    for name, slot in roll_services(ecs, settings, plans).items():
        drain(ecs, settings, name, slot, checked)
    run_task(ecs, settings, "schedule-bootstrap")
    run_task(ecs, settings, "schedule-bootstrap", PRUNE_COMMAND)


def _terraform_deploy(env: dict[str, str]) -> dict[str, Any]:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "container_deploy"],
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
        print(f"{LOG} {exc}")
        return 1
    print(f"{LOG} the services run their current task definitions and the schedules exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
