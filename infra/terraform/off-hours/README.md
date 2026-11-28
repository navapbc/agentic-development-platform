# off-hours

Module that scales the ECS services, the EKS deployments that connect to Aurora, and the EKS node groups to zero overnight, checks that nothing still holds a database connection, and restores them in the morning, so the Aurora cluster auto-pauses and the Fargate tasks and the node instances stop billing. Nothing is created unless `create_off_hours_schedule = true`. The function role is created whenever the toggle is on, and every other resource needs at least one listed service as well.

## What it provisions

- Two EventBridge Scheduler schedules, `<name_prefix>-off-hours-sleep` on `sleep_schedule` and `<name_prefix>-off-hours-wake` on `wake_schedule`, both in `timezone`. Each invokes the off-hours function with the run name and the scheduler's schedule ARN, scheduled time, execution ID, and attempt number, retries a failed delivery up to three times within 15 minutes, and then writes the event to the dead-letter queue.
- The `<name_prefix>-off-hours` Lambda function on python3.14, which runs the handlers of `functions/scale.py` and the step plan in `plan.json` that the module generates from the step lists below and packages with it. It traces through X-Ray, writes JSON logs at INFO to a log group encrypted under the alert key and kept for `log_retention_days`, and runs outside the VPC because it calls only AWS APIs and the RDS Data API.
- An asynchronous invocation configuration for the function with no retries, a 15-minute maximum event age, and the alert topic as its on-failure destination.
- The `<name_prefix>-off-hours-dead-letters` SQS queue, encrypted under the alert key, and the `<name_prefix>-off-hours-dead-letters` CloudWatch alarm, which publishes to the topic while the queue holds a message.
- The `/<name_prefix>/off-hours/wake-counts` SSM parameter, which holds the desired count of each ECS service, and the minimum and desired size of each node group, that the sleep scaled to zero. Terraform creates it as `{}` and ignores its value afterwards.
- The `<name_prefix>-off-hours-alerts` SNS topic, encrypted under its own customer-managed KMS key with rotation enabled, which receives an alert for each failed check, each failed run, and each step or delivery that never ran to completion. Its policy admits only the dead-letter alarm, and its key admits CloudWatch to encrypt that alarm's message.
- A function role and a scheduler role, described under [Access](#access).

The function role does not depend on the service list, so a caller can name it in a policy that the services depend on, such as the key policy and the resource policy of the monitor secret.

## Step execution

Each schedule invokes the function asynchronously with `{"Run": "sleep"}` or `{"Run": "wake"}` and the scheduler's context attributes. The function runs the first step of that run's plan and, when the step succeeds, invokes itself asynchronously with the same event, the next step number, and the run ID, which is the scheduler's execution ID. Each step therefore runs in its own invocation. The longest wait of a step is 540 seconds and the function times out at 600, so a step finishes within one invocation, and the run as a whole has no time limit. The last step counts the run as `Completed`. The function sets no reserved concurrency, because reserving any leaves an account whose concurrency quota is below 101 unable to create it, so two runs started together, such as a manual run during a scheduled one, run their steps side by side.

Lambda delivers an asynchronous invocation at least once, so a step can run twice, and each handler is safe to repeat. The sleep records only the services and node groups that run, the wake removes each count and size it restores, a node group update starts only when the node group lacks the target size, `scale-down` keeps an annotation it finds, `scale-up` removes the annotation it restores, and the checks and the health wait only read.

## Orchestration decision

The steps run as a chain of asynchronous invocations of one function rather than as SSM Automation documents or a Step Functions state machine.

- Runtime. SSM `aws:executeScript` offers no Python runtime as new as the version `.python-version` pins for the whole platform. The function runs the pinned runtime.
- Step length. An `aws:invokeLambdaFunction` step runs for at most 300 seconds, shorter than the 540-second waits, and does not fail the step when the function raises. Each invocation of the chain gets the full function timeout.
- Size. SSM caps a document at 64 KB, which the handlers inlined in each step exceed. The plan travels in the function package, which has no such limit.
- Moving parts. A state machine adds a definition, a role, and a log configuration for a linear run of at most six steps. The chain adds only the function's permission to invoke itself.

## Schedule

`sleep_schedule` defaults to `cron(0 20 ? * * *)`, 20:00 every day, and `wake_schedule` to `cron(0 7 ? * MON-FRI *)`, 07:00 Monday through Friday, both in `timezone`, which defaults to `America/Chicago`.

## Sleep

| Step | Handler | Effect |
|---|---|---|
| `StopServices` | `sleep` | Records the desired count of each running service in `services` and scales it to zero, then waits up to 540 seconds until every task has reached `DEPROVISIONING` or `STOPPED`. |
| `ScaleDownDeployments` | `scale_deployments` | Runs the eks-deployer task with `scale-down` and the deployments in `eks_deployer`. The task records each deployment's replica count in the `off-hours.agentic-dev-platform/replicas` annotation, scales it to zero, and waits up to 300 seconds until none of its pods runs. Only with `eks_deployer`. |
| `CheckBeforeFoundation` | `check` | Fails while a service in `services` runs a task, an `eks_deployer` deployment runs a pod, or `pg_stat_activity` lists a client connection of a user outside `foundation_database_users`. |
| `ScaleDownNodeGroups` | `sleep_node_groups` | Records the minimum and desired size of each node group in `node_group_arns` that runs nodes, sets both to zero, and waits up to 540 seconds for each scaling update to succeed. EKS then drains and terminates the nodes. Only with `node_group_arns`. |
| `StopFoundationServices` | `sleep` | Records and scales `foundation_services` to zero, and waits up to 540 seconds for their tasks to exit. |
| `CheckBeforePause` | `check` | Fails while any listed service runs a task, an `eks_deployer` deployment runs a pod, or `pg_stat_activity` lists any client connection. |

A tenant worker that is running an activity when the sleep starts keeps its pod until the activity finishes, for up to its 2460-second termination grace, so the check before the foundation fails and alerts while that pod runs, and the platform stays awake until the next sleep or a manual run from `ScaleDownDeployments`.

ECS lowers a service's running count as soon as a task starts to stop, while its containers keep running through the load balancer drain and the stop timeout, so the wait reads the tasks rather than the count. A terminating pod counts as running for the same reason.

The connection check reads `pg_stat_activity` in the `postgres` database through the RDS Data API as the user of the `database.monitor_secret_arn` secret, which is a member of `pg_monitor`. It counts only `client backend` sessions, and leaves out the RDS internal users `rdsadmin` and `rdsrepladmin` and its own user. The ECS and connection checks repeat every 10 seconds for up to 120 seconds, so a connection that closes as its task exits does not fail the check. A cluster that answers `DatabaseResumingException` was paused and holds no connection. Without `database`, the check reads no connection, and without `eks_deployer`, it reads no pod.

## Wake

| Step | Handler | Effect |
|---|---|---|
| `ScaleUpNodeGroups` | `wake_node_groups` | Restores the recorded minimum and desired size of each node group in `node_group_arns` and waits up to 540 seconds for each scaling update to succeed. It runs first, so the nodes boot while the foundation services start and are ready before the deployments scale up. Only with `node_group_arns`. |
| `StartFoundationServices` | `wake` | Restores the recorded count of each service in `foundation_services` and waits up to `foundation_wait_seconds` for their tasks to run. |
| `AwaitFoundationHealth` | `await_healthy` | Waits up to 540 seconds until each service in `health_checked_services` runs its desired count and every task reports `HEALTHY`. The Temporal server's container health check probes the frontend's HTTP API, so no worker starts before the frontend answers. Only with `health_checked_services`. |
| `StartServices` | `wake` | Restores the recorded count of each service in `services`. |
| `ScaleUpDeployments` | `scale_deployments` | Runs the eks-deployer task with `scale-up`, which restores the replica count each annotation recorded, removes the annotation, and waits up to 5 minutes for each rollout. Only with `eks_deployer`. |

The sleep records only the services and deployments that run, and the wake restores only those it finds recorded and removes the record. A service or deployment at zero when the sleep runs, such as a drained Temporal worker slot, therefore stays at zero after the wake, and a second sleep keeps the counts the first recorded. A deployment during the night starts its services and pods as usual, and the next sleep records and stops them again. A `terraform apply` during the night sets each node group back to the sizes its configuration names, so the nodes run until the next sleep, which records those sizes and scales them down again. Run the sleep from the node group step after such an apply, as [Running it outside the schedule](#running-it-outside-the-schedule) shows.

## Failure and alerts

A step that raises ends the run. The function logs the failure, counts the run as `Failed`, and publishes an alert to the topic that names the run ID, the step, and the error, with the `aws logs filter-log-events --log-group-name <log group> --filter-pattern '{ $.run_id = "<run id>" }'` command that lists the run's audit records. It then returns without raising, so Lambda does not publish the invocation a second time. A step that cannot invoke the function for the next step, such as under a sustained throttle, fails the same way. A failed check stops the run before any later step, so the services that remain keep running and the cluster stays awake. The check publishes its own alert first, with one line for each running ECS service, each running pod as `namespace/name`, and each connection with its process ID, user, database, client address, and application name. A failed `AwaitFoundationHealth` leaves the other services and the EKS deployments at zero.

A step that the function cannot finish, such as one that times out, one whose runtime crashes, or one whose event waits longer than 15 minutes under a throttle, records nothing itself. Lambda publishes its invocation record, with the event and the error, to the topic, and does not retry it. When the scheduler cannot invoke the function, such as after the function is deleted or loses the scheduler's permission, it retries for up to 15 minutes and then writes the event to the dead-letter queue with the error in its message attributes, and the alarm publishes to the topic within five minutes. `aws sqs receive-message --queue-url <dead_letter_queue_url> --message-attribute-names All` shows the error.

Subscribe a responder to the topic, for example `aws sns subscribe --topic-arn <alert_topic_arn> --protocol email --notification-endpoint ops@example.gov`.

## Records

The function writes these metrics to the `<name_prefix>/OffHours` CloudWatch namespace, which an alarm can watch.

| Metric | Dimension | Value |
|---|---|---|
| `Completed` | `Run`, `sleep` or `wake` | 1 for each run whose last step succeeded |
| `Failed` | `Run`, `sleep` or `wake` | 1 for each run that a step failed |
| `CheckFindings` | `Check`, `BeforeFoundation` or `BeforePause` | The number of running services, pods, and connections the check found |

Each step writes an audit record to the function log group when it starts and another when it ends, and the last step of a successful run writes a third. Each record names `event`, which is `off_hours_step`, `run`, `run_id`, `step`, `step_number`, `steps`, `handler`, and `trigger`, which is `schedule`, `chained`, or `direct`. The records of a scheduled run also name `ScheduleArn`, `ScheduledTime`, `SchedulerExecutionId`, and `Attempt`. The record at the end of a step adds `phase`, which is `succeeded`, `failed`, or `handoff_failed`, `outcome`, `duration_ms`, and the handler's `result`, the `next_step`, or the `error`. The Lambda JSON log format adds the timestamp, the level, and the request ID. A trail that lists the function in the `cloudtrail` module's `data_event_function_arns`, as `private-bedrock` does, records each `Invoke` call with the identity that made it, whether the scheduler, the function itself, or an operator. The eks-deployer log group keeps each scale and check task's log.

## EKS scaling decision

The EKS deployments scale through the eks-deployer one-shot ECS task, which the off-hours function starts with `scale-down`, `scale-up`, or `check-stopped`, rather than by calling the Kubernetes API from the function itself.

- Cluster access. The deployer's task role already holds an access entry, and its security group is already admitted to the private API endpoint, so the schedule adds no Kubernetes principal, no access entry, no RBAC binding, and no network path. A Lambda function would need a new role with its own access entry and RBAC binding.
- Image and code. The deployer image already carries kubectl and the token helper and is built, scanned, and signed through the platform image pipeline. A Lambda function would need a packaged Kubernetes client and a token signer as a new artifact.
- Private networking. The deployer runs in the management subnets. A Lambda function would need a VPC attachment and a security group admitted to the endpoint.
- FedRAMP posture. The schedule adds no identity with cluster access and no image to the inventory. Each run is a `RunTask` call with `startedBy = off-hours` in CloudTrail, and the task writes to the KMS-encrypted deployer log group. The deployer's access entry grants cluster administration, which exceeds what scaling needs. The same entry serves the deploy and the credential restart, so the schedule widens no grant.
- Cost. A Fargate task of 0.5 vCPU and 1 GB runs for a few minutes, three times each sleep and once each wake. A Lambda function would cost about the same.

## Connection check decision

The check reads `pg_stat_activity` through the RDS Data API with the credential of a dedicated `monitor` role, which the [secret store](../secret-store) generates, rotates, and makes a member of `pg_monitor` at the role bootstrap. The off-hours function runs outside the VPC with no route to the database port, and the Data API needs none. IAM database authentication would need that route and a database driver, and the RDS-managed master secret grants far more than reading the activity view. The function role may call `ExecuteStatement` only on the cluster and read only the monitor secret, and the secret's resource policy and key policy admit the role by name.

## Access

The function role may only:

- describe and update the listed ECS services, and list and describe the tasks of `cluster_arns` and of the deployer's cluster
- describe the node groups in `node_group_arns` and update their scaling configuration, and read the updates of their clusters, because EKS authorizes `DescribeUpdate` of a node group update against the cluster ARN
- read and write the wake-counts parameter
- run the `eks_deployer.task_family` task definition on its cluster, pass its execution and task roles to ECS, and read its log streams
- call the Data API `ExecuteStatement` on `database.cluster_arn` and read `database.monitor_secret_arn`
- publish to the alert topic and use its key through SNS
- put metric data in the `<name_prefix>/OffHours` namespace
- write to its own log group and send trace segments
- invoke itself for the next step

The scheduler role may only invoke the off-hours function, send to the dead-letter queue, and use the alert key through SQS.

## Credential rotation

The secret store rotates a single database or cache user, so a rotation refuses the new connections of a service that is still running until its restart rule replaces its tasks. The secret store rotates on the first Sunday of every third month between 06:00 and 09:00 UTC by default, which falls inside the default window. A rotation then finds every service and deployment at zero, the restart rule redeploys each one at zero, and the wake starts it with the new password. `rotation_schedule_expression` takes the secret store's schedule, and the module refuses a `wake_schedule` that wakes on a Saturday or a Sunday while that schedule rotates on a Sunday.

## Running it outside the schedule

Invoke the function asynchronously with the run name, for example `aws lambda invoke --function-name agentic-platform-off-hours --invocation-type Event --cli-binary-format raw-in-base64-out --payload '{"Run":"wake"}' response.json`, to wake the services early or put them to sleep before a manual rotation. Add `"Step"` with a zero-based step number to start a run at that step, for example `{"Run":"sleep","Step":3}` for `ScaleDownNodeGroups` when the root also runs EKS deployments, after a failed step has been fixed. The run ID of a manual run is the request ID of its first invocation, and `aws logs filter-log-events --log-group-name /aws/lambda/agentic-platform-off-hours --filter-pattern '{ $.trigger = "direct" }'` lists the records of manual runs.

## Outputs

| Output | Value |
|---|---|
| `function_role_arn` | The role the off-hours function runs as, which reads the monitor secret. Null when the toggle is off |
| `function_name` | The off-hours Lambda function. Null when disabled |
| `function_arn` | The ARN of the off-hours function, known at plan, for the CloudTrail data-event selector. Null when disabled |
| `dead_letter_queue_url` | The queue holding each schedule delivery the scheduler could not hand to the function. Null when disabled |
| `wake_counts_parameter` | The parameter holding the counts the wake restores |
| `alert_topic_arn` | The topic a failed check or run publishes to |
| `metric_namespace` | The CloudWatch namespace of the run and check metrics |

## Tests

`tests/off_hours.tftest.hcl` runs the module against a mocked provider and asserts that the disabled module creates nothing, that the schedules invoke the function in the time zone with the context attributes the audit records name, that the foundation services stop last and start first, that the sleep scales the EKS deployments down, checks, and scales the node groups down before the foundation services stop and checks again after, that the wake starts the node groups first and awaits the Temporal server's health before it starts the services and the deployments, that each run records its result to the namespace, the topic, and the log group, that the function role reaches only the deployer, its log group, the node groups and their clusters' updates, the cluster's Data API, the monitor secret, the topic, the namespace, and the function itself, that each step finishes its longest wait within the function timeout, that the function writes encrypted JSON logs, that a step the function cannot finish alerts without a retry, that an undeliverable schedule alarms from the dead-letter queue, that the scheduler role may only invoke the function and write its dead letters, that the function ARN is known at plan, and that a weekend wake is refused only while the rotation runs on a Sunday. `tests/unit/test_off_hours_scale.py` runs `functions/scale.py` against fake ECS, EKS, CloudWatch Logs, Data API, SNS, CloudWatch, SSM, and Lambda clients, and `tests/unit/test_eks_deploy.py` runs the `scale-down`, `scale-up`, and `check-stopped` commands against a fake kubectl. Run them with `inv terraform-test` and `inv ci`.
