"""Scale ECS services to zero for the night and restore them in the morning.

SSM Automation runs `sleep` and `wake` through aws:executeScript. Sleep records the desired count
of every listed service that runs tasks in an SSM parameter and sets it to zero. Wake restores the
recorded counts of the listed services and removes them from the record, so a service that ran no
tasks at sleep, such as a drained worker slot, stays at zero.
"""

import json
import time

import boto3

POLL_SECONDS = 10
# A task whose containers have exited holds no connection, though ECS still finishes stopping it.
EXITED = ("DEPROVISIONING", "STOPPED")


def _cluster(service_arn):
    return service_arn.split("/")[1]


def _describe(ecs, service_arns):
    by_cluster = {}
    for arn in service_arns:
        by_cluster.setdefault(_cluster(arn), []).append(arn)
    described = {}
    for cluster, arns in by_cluster.items():
        for start in range(0, len(arns), 10):
            response = ecs.describe_services(cluster=cluster, services=arns[start : start + 10])
            for service in response["services"]:
                described[service["serviceArn"]] = service
    return described


def _stopped(ecs, service):
    """Report whether a service at zero has no task left that still runs its containers.

    The running count drops as soon as a task starts to stop, while its containers keep running
    through the load balancer drain and the stop timeout.
    """
    if service["runningCount"] or service["pendingCount"]:
        return False
    cluster = _cluster(service["serviceArn"])
    stopping = ecs.list_tasks(
        cluster=cluster, serviceName=service["serviceName"], desiredStatus="STOPPED"
    )["taskArns"]
    if not stopping:
        return True
    tasks = ecs.describe_tasks(cluster=cluster, tasks=stopping[:100])["tasks"]
    return all(task["lastStatus"] in EXITED for task in tasks)


def _load(ssm, parameter):
    return json.loads(ssm.get_parameter(Name=parameter)["Parameter"]["Value"] or "{}")


def _save(ssm, parameter, counts):
    ssm.put_parameter(Name=parameter, Value=json.dumps(counts, sort_keys=True), Overwrite=True)


def _wait(ecs, targets, settled, seconds):
    """Poll until every target service satisfies `settled`, returning those that never did."""
    deadline = time.monotonic() + seconds
    pending = list(targets)
    while pending:
        described = _describe(ecs, pending)
        pending = [arn for arn in pending if not settled(ecs, described[arn])]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    return pending


def sleep(events, context, ecs=None, ssm=None):
    ecs = ecs or boto3.client("ecs")
    ssm = ssm or boto3.client("ssm")
    services = events["Services"]
    recorded = _load(ssm, events["Parameter"])
    running = {
        arn: service["desiredCount"]
        for arn, service in _describe(ecs, services).items()
        if service["desiredCount"] > 0
    }
    recorded.update(running)
    _save(ssm, events["Parameter"], recorded)
    for arn in running:
        ecs.update_service(cluster=_cluster(arn), service=arn, desiredCount=0)
    pending = _wait(
        ecs,
        running,
        _stopped,
        events.get("WaitSeconds", 0),
    )
    return {"stopped": sorted(running), "still_running": pending}


def wake(events, context, ecs=None, ssm=None):
    ecs = ecs or boto3.client("ecs")
    ssm = ssm or boto3.client("ssm")
    services = set(events["Services"])
    recorded = _load(ssm, events["Parameter"])
    targets = {arn: count for arn, count in recorded.items() if arn in services}
    for arn, count in targets.items():
        ecs.update_service(cluster=_cluster(arn), service=arn, desiredCount=count)
    _save(ssm, events["Parameter"], {k: v for k, v in recorded.items() if k not in targets})
    pending = _wait(
        ecs,
        targets,
        lambda ecs, service: service["runningCount"] >= service["desiredCount"],
        events.get("WaitSeconds", 0),
    )
    return {"started": sorted(targets), "not_yet_running": pending}
