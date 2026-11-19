"""Scale the platform to zero for the night, check that it let go of the database, and restore it.

The off-hours Lambda function runs this module. EventBridge Scheduler invokes it with the run
to start, `sleep` or `wake`, and `handler` runs one step of that run's plan from `plan.json` in
each invocation. A step that succeeds invokes the function asynchronously for the next step, a
step that fails records the run as Failed, and the last step records it as Completed. Every step
writes a structured audit record when it starts and when it ends.

- `sleep` records the desired count of every listed ECS service that runs tasks in an SSM
  parameter and sets it to zero. `wake` restores the recorded counts of the listed services and
  removes them from the record, so a service that ran no tasks at sleep, such as a drained worker
  slot, stays at zero.
- `scale_deployments` runs the eks-deployer task with `scale-down` or `scale-up`, which records
  each EKS deployment's replica count in an annotation and restores it.
- `await_healthy` waits until every task of the listed ECS services passes its container health
  check, such as the Temporal server's probe of its frontend.
- `check` fails, and publishes an alert naming each finding, while a listed ECS service runs a
  task, a listed EKS deployment runs a pod, or pg_stat_activity lists a client connection outside
  the RDS internal sessions and the allowed users.
- `record` writes the result of a run to CloudWatch, and publishes an alert when the run failed.
  It runs at the end of a run, not as a step.
"""

import json
import logging
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

POLL_SECONDS = 10
PLAN = Path(__file__).with_name("plan.json")
log = logging.getLogger("off_hours")
# A task whose containers have exited holds no connection, though ECS still finishes stopping it.
EXITED = ("DEPROVISIONING", "STOPPED")
RUNNING_POD = "[eks-deployer] running pod "
# The Data API session of the check itself runs as current_user.
CONNECTIONS = (
    "SELECT pid, usename, datname, application_name, host(client_addr) AS client_addr "
    "FROM pg_stat_activity "
    "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() "
    "AND usename <> current_user AND usename NOT IN ('rdsadmin', 'rdsrepladmin') "
    "ORDER BY pid"
)


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


def _healthy(ecs, service):
    """Report whether a service runs its desired count, each task passing its health check."""
    if not service["desiredCount"]:
        return False
    cluster = _cluster(service["serviceArn"])
    arns = ecs.list_tasks(
        cluster=cluster, serviceName=service["serviceName"], desiredStatus="RUNNING"
    )["taskArns"]
    if len(arns) < service["desiredCount"]:
        return False
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns[:100])["tasks"]
    return all(
        task["lastStatus"] == "RUNNING" and task.get("healthStatus") == "HEALTHY" for task in tasks
    )


def await_healthy(events, context, ecs=None):
    ecs = ecs or boto3.client("ecs")
    services = events["Services"]
    pending = _wait(ecs, services, _healthy, events.get("WaitSeconds", 0))
    if pending:
        names = ", ".join(sorted(arn.rsplit("/", 1)[-1] for arn in pending))
        raise RuntimeError(f"{names} did not report healthy, so the other services stay at zero")
    return {"healthy": sorted(services)}


def _log_lines(logs, group, stream):
    lines, token = [], None
    while True:
        page = logs.get_log_events(
            logGroupName=group,
            logStreamName=stream,
            startFromHead=True,
            **({"nextToken": token} if token else {}),
        )
        lines.extend(event["message"] for event in page["events"])
        if not page["events"] or page.get("nextForwardToken") in (None, token):
            return lines
        token = page["nextForwardToken"]


def run_deployer(ecs, logs, deployer, command, seconds):
    """Run the eks-deployer task with `command`, returning its exit code and its log lines."""
    started = ecs.run_task(
        cluster=deployer["Cluster"],
        taskDefinition=deployer["TaskFamily"],
        launchType="FARGATE",
        startedBy="off-hours",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": deployer["Subnets"],
                "securityGroups": deployer["SecurityGroups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": deployer["Container"], "command": command}]},
    )
    if started.get("failures") or not started.get("tasks"):
        failures = started.get("failures")
        raise RuntimeError(f"the eks-deployer task did not start, failures {failures}")
    task_arn = started["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    deadline = time.monotonic() + seconds
    while True:
        task = ecs.describe_tasks(cluster=deployer["Cluster"], tasks=[task_arn])["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"the eks-deployer task {task_id} ran {command[0]} past {seconds}s")
        time.sleep(POLL_SECONDS)
    container = deployer["Container"]
    exit_code = next(c for c in task["containers"] if c["name"] == container).get("exitCode")
    try:
        lines = _log_lines(logs, deployer["LogGroup"], f"{container}/{container}/{task_id}")
    except logs.exceptions.ResourceNotFoundException:
        lines = []
    return exit_code, lines


def _running_pods(lines):
    return sorted(line.split(RUNNING_POD, 1)[1].strip() for line in lines if RUNNING_POD in line)


def scale_deployments(events, context, ecs=None, logs=None):
    ecs = ecs or boto3.client("ecs")
    logs = logs or boto3.client("logs")
    command = [events["Command"], *events["Deployments"]]
    exit_code, lines = run_deployer(ecs, logs, events["Deployer"], command, events["WaitSeconds"])
    if exit_code != 0:
        raise RuntimeError(f"eks-deployer {events['Command']} exited with {exit_code}")
    return {"deployments": events["Deployments"], "still_running": _running_pods(lines)}


def _service_findings(ecs, services):
    described = _describe(ecs, services)
    return [
        f"ECS service {described[arn]['serviceName']} runs a task"
        for arn in services
        if not _stopped(ecs, described[arn])
    ]


def _connection_findings(rds_data, database):
    try:
        response = rds_data.execute_statement(
            resourceArn=database["ClusterArn"],
            secretArn=database["SecretArn"],
            database="postgres",
            sql=CONNECTIONS,
            formatRecordsAs="JSON",
        )
    except ClientError as error:
        # A paused cluster holds no connection, though the call starts its resume.
        if error.response["Error"]["Code"] == "DatabaseResumingException":
            return []
        raise
    allowed = set(database.get("AllowedUsers", []))
    return [
        f"Database connection {row['pid']} of {row['usename']} to {row['datname']} "
        f"from {row.get('client_addr') or 'a local socket'} "
        f"as {row.get('application_name') or 'no application'}"
        for row in json.loads(response.get("formattedRecords") or "[]")
        if row["usename"] not in allowed
    ]


def _metric(cloudwatch, events, name, value, dimension, dimension_value):
    cloudwatch.put_metric_data(
        Namespace=events["Namespace"],
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": [{"Name": dimension, "Value": dimension_value}],
                "Value": value,
                "Unit": "Count",
            }
        ],
    )


def _alert(sns, events, subject, lines):
    sns.publish(TopicArn=events["Topic"], Subject=subject[:100], Message="\n".join(lines))


def check(events, context, ecs=None, logs=None, rds_data=None, sns=None, cloudwatch=None):
    """Fail, and alert with every finding, while anything still runs or holds a connection.

    The deployer check runs once. The ECS and database checks repeat for up to WaitSeconds, so a
    connection that closes as its task exits does not fail the check.
    """
    ecs = ecs or boto3.client("ecs")
    findings = []
    deployer = events.get("Deployer")
    if deployer and events.get("Deployments"):
        exit_code, lines = run_deployer(
            ecs,
            logs or boto3.client("logs"),
            deployer,
            ["check-stopped", *events["Deployments"]],
            events["DeployerWaitSeconds"],
        )
        pods = _running_pods(lines)
        findings.extend(f"EKS pod {pod} is running" for pod in pods)
        if exit_code != 0 and not pods:
            findings.append(f"The eks-deployer check-stopped task exited with {exit_code}")
    database = events.get("Database") or {}
    reads_connections = bool(database.get("ClusterArn"))
    if reads_connections:
        rds_data = rds_data or boto3.client("rds-data")
    deadline = time.monotonic() + events.get("WaitSeconds", 0)
    while True:
        remaining = _service_findings(ecs, events["Services"])
        if reads_connections:
            remaining.extend(_connection_findings(rds_data, database))
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    findings.extend(remaining)
    cloudwatch = cloudwatch or boto3.client("cloudwatch")
    _metric(cloudwatch, events, "CheckFindings", len(findings), "Check", events["Check"])
    if findings:
        _alert(
            sns or boto3.client("sns"),
            events,
            f"Off-hours {events['Run']} check {events['Check']} failed",
            [
                f"The off-hours {events['Run']} stopped at the {events['Check']} check. "
                "The database cluster stays awake while the following run or hold a connection.",
                *findings,
            ],
        )
        raise RuntimeError(f"{events['Check']} found {len(findings)}. {'. '.join(findings)}.")
    return {"findings": []}


def record(events, context, cloudwatch=None, sns=None):
    """Count the run as Completed or Failed, and alert on a failed run."""
    result = events["Result"]
    _metric(cloudwatch or boto3.client("cloudwatch"), events, result, 1, "Run", events["Run"])
    if result == "Failed":
        run_id = events["RunId"]
        _alert(
            sns or boto3.client("sns"),
            events,
            f"Off-hours {events['Run']} failed",
            [
                f"The off-hours {events['Run']} run {run_id} failed at step "
                f"{events['Step']}. {events['Error']}",
                f"aws logs filter-log-events --log-group-name {events['LogGroup']} "
                f"--filter-pattern '{{ $.run_id = \"{run_id}\" }}' lists the audit records of "
                "each step of the run.",
            ],
        )
    return {"result": result}


HANDLERS = {
    "sleep": sleep,
    "wake": wake,
    "await_healthy": await_healthy,
    "scale_deployments": scale_deployments,
    "check": check,
}
# EventBridge Scheduler fills these from its context attributes, so an audit record names the
# schedule, the scheduled time, and the delivery attempt that started the run.
TRIGGER_FIELDS = ("ScheduleArn", "ScheduledTime", "SchedulerExecutionId", "Attempt")


def load_plan():
    return json.loads(PLAN.read_text(encoding="utf-8"))


def _trigger(event):
    if event.get("Step"):
        return "chained"
    return "schedule" if event.get("ScheduleArn") else "direct"


def handler(event, context, plan=None, lambda_client=None, cloudwatch=None, sns=None):
    """Run one step of the sleep or wake plan, then invoke this function for the next step.

    Each step gets its own invocation, so a run is not bounded by the function timeout. A failed
    step records the run as Failed and ends it, and the last step records it as Completed.
    """
    plan = plan or load_plan()
    run = event.get("Run")
    if run not in plan:
        raise ValueError(f"no off-hours run named {run!r}")
    runbook = plan[run]
    steps = runbook["Steps"]
    number = int(event.get("Step", 0))
    if not 0 <= number < len(steps):
        raise ValueError(f"the off-hours {run} run has no step {number}")
    step = steps[number]
    run_id = event.get("RunId") or event.get("SchedulerExecutionId") or context.aws_request_id
    audit = {
        "event": "off_hours_step",
        "run": run,
        "run_id": run_id,
        "step": step["name"],
        "step_number": number + 1,
        "steps": len(steps),
        "handler": step["handler"],
        "trigger": _trigger(event),
        **{field: event[field] for field in TRIGGER_FIELDS if event.get(field)},
    }
    log.info("off-hours step started", extra={**audit, "phase": "started"})
    started = time.monotonic()

    def finish(phase, outcome, **fields):
        duration_ms = round((time.monotonic() - started) * 1000)
        level = logging.INFO if outcome == "success" else logging.ERROR
        log.log(
            level,
            f"off-hours step {phase}",
            extra={
                **audit,
                "phase": phase,
                "outcome": outcome,
                "duration_ms": duration_ms,
                **fields,
            },
        )

    def fail(phase, error):
        message = f"{type(error).__name__}: {error}"
        finish(phase, "failure", error=message)
        failed = {**runbook["Record"], "Result": "Failed", "RunId": run_id, "Step": step["name"]}
        record({**failed, "Error": message}, context, cloudwatch, sns)
        return {"result": "Failed", "run_id": run_id, "step": step["name"]}

    try:
        result = HANDLERS[step["handler"]](step["payload"], context)
    except Exception as error:  # noqa: BLE001
        return fail("failed", error)
    if number + 1 < len(steps):
        following = {**event, "Step": number + 1, "RunId": run_id}
        try:
            (lambda_client or boto3.client("lambda")).invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",
                Payload=json.dumps(following).encode(),
            )
        except Exception as error:  # noqa: BLE001
            return fail("handoff_failed", error)
        finish("succeeded", "success", result=result, next_step=steps[number + 1]["name"])
        return {"result": "Continued", "run_id": run_id, "step": step["name"]}
    finish("succeeded", "success", result=result)
    record({**runbook["Record"], "Result": "Completed", "RunId": run_id}, context, cloudwatch, sns)
    log.info("off-hours run completed", extra={**audit, "phase": "completed", "outcome": "success"})
    return {"result": "Completed", "run_id": run_id}
