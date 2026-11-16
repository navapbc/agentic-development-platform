# off-hours

Module that scales the ECS services and the EKS deployments that connect to Aurora to zero overnight, checks that nothing still holds a database connection, and restores them in the morning, so the Aurora cluster auto-pauses and the Fargate tasks stop billing. Nothing is created unless `create_off_hours_schedule = true`. The automation and function roles are created whenever the toggle is on, and every other resource needs at least one listed service as well.

## What it provisions

- Two EventBridge Scheduler schedules, `<name_prefix>-off-hours-sleep` on `sleep_schedule` and `<name_prefix>-off-hours-wake` on `wake_schedule`, both in `timezone`. Each starts an SSM Automation document.
- The `<name_prefix>-off-hours-sleep` and `<name_prefix>-off-hours-wake` Automation documents. Each step runs `functions/invoke.py` through `aws:executeScript`, which invokes the off-hours function with the handler the step names and its input, and fails the step when the function raises.
- The `<name_prefix>-off-hours` Lambda function, which runs the handlers of `functions/scale.py`, with active tracing, and a log group encrypted under the alert key and kept for `log_retention_days`. It runs outside the VPC, as `aws:executeScript` does, because it calls only AWS APIs and the RDS Data API.
- The `/<name_prefix>/off-hours/wake-counts` SSM parameter, which holds the desired count of each ECS service the sleep scaled to zero. Terraform creates it as `{}` and ignores its value afterwards.
- The `<name_prefix>-off-hours-alerts` SNS topic, encrypted under its own customer-managed KMS key with rotation enabled, which receives an alert for each failed check and each failed run.
- An automation role, a function role, and a scheduler role, described under [Access](#access).

The function role does not depend on the service list, so a caller can name it in a policy that the services depend on, such as the key policy and the resource policy of the monitor secret.

## Step execution

SSM caps an Automation document at 64 KB. Inlining `functions/scale.py` in every `aws:executeScript` step would exceed it, so the handlers run in one Lambda function and each step carries only the short `functions/invoke.py`. An `aws:invokeLambdaFunction` step runs for at most 300 seconds, shorter than the 540-second waits of the scale handlers, so each step invokes the function from `aws:executeScript`, which allows 600 seconds. The function times out at 560 seconds, the invoke script waits 590 seconds for its response, which leaves room for a cold start's init phase that the function timeout does not count, and the step times out at 600 seconds, so a function timeout surfaces as a failed step that `RecordFailure` records. The invoke script retries a throttled invocation for up to 60 seconds, so an account at its Lambda concurrency limit delays a step, and its `RecordFailure`, instead of failing them. The function sets no reserved concurrency, because reserving any leaves an account whose concurrency quota is below 101 unable to create it, so two runs started together, such as a manual run during a scheduled one, run their steps side by side, as they did under `aws:executeScript`. `tests/off_hours.tftest.hcl` asserts that each document stays under 64 KB.

## Schedule

`sleep_schedule` defaults to `cron(0 20 ? * * *)`, 20:00 every day, and `wake_schedule` to `cron(0 7 ? * MON-FRI *)`, 07:00 Monday through Friday, both in `timezone`, which defaults to `America/Chicago`.

## Sleep

| Step | Handler | Effect |
|---|---|---|
| `StopServices` | `sleep` | Records the desired count of each running service in `services` and scales it to zero, then waits up to 540 seconds until every task has reached `DEPROVISIONING` or `STOPPED`. |
| `ScaleDownDeployments` | `scale_deployments` | Runs the eks-deployer task with `scale-down` and the deployments in `eks_deployer`. The task records each deployment's replica count in the `off-hours.agentic-dev-platform/replicas` annotation, scales it to zero, and waits up to 300 seconds until none of its pods runs. Only with `eks_deployer`. |
| `CheckBeforeFoundation` | `check` | Fails while a service in `services` runs a task, an `eks_deployer` deployment runs a pod, or `pg_stat_activity` lists a client connection of a user outside `foundation_database_users`. |
| `StopFoundationServices` | `sleep` | Records and scales `foundation_services` to zero, and waits up to 540 seconds for their tasks to exit. |
| `CheckBeforePause` | `check` | Fails while any listed service runs a task, an `eks_deployer` deployment runs a pod, or `pg_stat_activity` lists any client connection. |
| `RecordSuccess` | `record` | Counts the run as `Completed` and ends it. |

ECS lowers a service's running count as soon as a task starts to stop, while its containers keep running through the load balancer drain and the stop timeout, so the wait reads the tasks rather than the count. A terminating pod counts as running for the same reason.

The connection check reads `pg_stat_activity` in the `postgres` database through the RDS Data API as the user of the `database.monitor_secret_arn` secret, which is a member of `pg_monitor`. It counts only `client backend` sessions, and leaves out the RDS internal users `rdsadmin` and `rdsrepladmin` and its own user. The ECS and connection checks repeat every 10 seconds for up to 120 seconds, so a connection that closes as its task exits does not fail the check. A cluster that answers `DatabaseResumingException` was paused and holds no connection. Without `database`, the check reads no connection, and without `eks_deployer`, it reads no pod.

## Wake

| Step | Handler | Effect |
|---|---|---|
| `StartFoundationServices` | `wake` | Restores the recorded count of each service in `foundation_services` and waits up to `foundation_wait_seconds` for their tasks to run. |
| `AwaitFoundationHealth` | `await_healthy` | Waits up to 540 seconds until each service in `health_checked_services` runs its desired count and every task reports `HEALTHY`. The Temporal server's container health check probes the frontend's HTTP API, so no worker starts before the frontend answers. Only with `health_checked_services`. |
| `StartServices` | `wake` | Restores the recorded count of each service in `services`. |
| `ScaleUpDeployments` | `scale_deployments` | Runs the eks-deployer task with `scale-up`, which restores the replica count each annotation recorded, removes the annotation, and waits up to 5 minutes for each rollout. Only with `eks_deployer`. |
| `RecordSuccess` | `record` | Counts the run as `Completed` and ends it. |

The sleep records only the services and deployments that run, and the wake restores only those it finds recorded and removes the record. A service or deployment at zero when the sleep runs, such as a drained Temporal worker slot, therefore stays at zero after the wake, and a second sleep keeps the counts the first recorded. A deployment during the night starts its services and pods as usual, and the next sleep records and stops them again.

## Failure and alerts

Every step but the record steps goes to `RecordFailure` when it fails. `RecordFailure` counts the run as `Failed` and publishes an alert to the topic that names the execution, and `aws ssm get-automation-execution --automation-execution-id <id>` names the step that failed. A failed check stops the run before any later step, so the services that remain keep running and the cluster stays awake. The check publishes its own alert first, with one line for each running ECS service, each running pod as `namespace/name`, and each connection with its process ID, user, database, client address, and application name. A failed `AwaitFoundationHealth` leaves the other services and the EKS deployments at zero.

Subscribe a responder to the topic, for example `aws sns subscribe --topic-arn <alert_topic_arn> --protocol email --notification-endpoint ops@example.gov`.

## Records

The functions write these metrics to the `<name_prefix>/OffHours` CloudWatch namespace, which an alarm can watch.

| Metric | Dimension | Value |
|---|---|---|
| `Completed` | `Document`, `sleep` or `wake` | 1 for each run that reached `RecordSuccess` |
| `Failed` | `Document`, `sleep` or `wake` | 1 for each run that reached `RecordFailure` |
| `CheckFindings` | `Check`, `BeforeFoundation` or `BeforePause` | The number of running services, pods, and connections the check found |

The SSM execution history keeps each step's output, and the eks-deployer log group keeps each scale and check task's log.

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

The automation role may only invoke the off-hours function.

The function role may only:

- describe and update the listed ECS services, and list and describe the tasks of `cluster_arns` and of the deployer's cluster
- read and write the wake-counts parameter
- run the `eks_deployer.task_family` task definition on its cluster, pass its execution and task roles to ECS, and read its log streams
- call the Data API `ExecuteStatement` on `database.cluster_arn` and read `database.monitor_secret_arn`
- publish to the alert topic and use its key through SNS
- put metric data in the `<name_prefix>/OffHours` namespace
- write to its own log group and send trace segments

The scheduler role may only start the two documents and pass the automation role to SSM.

## Credential rotation

The secret store rotates a single database or cache user, so a rotation refuses the new connections of a service that is still running until its restart rule replaces its tasks. The secret store rotates on the first Sunday of every third month between 06:00 and 09:00 UTC by default, which falls inside the default window. A rotation then finds every service and deployment at zero, the restart rule redeploys each one at zero, and the wake starts it with the new password. `rotation_schedule_expression` takes the secret store's schedule, and the module refuses a `wake_schedule` that wakes on a Saturday or a Sunday while that schedule rotates on a Sunday.

## Running it outside the schedule

Start either document with the automation role from the outputs, for example `aws ssm start-automation-execution --document-name agentic-platform-off-hours-wake --parameters AutomationAssumeRole=<automation_role_arn>`, to wake the services early or put them to sleep before a manual rotation.

## Outputs

| Output | Value |
|---|---|
| `documents` | The sleep and wake document names. Empty when disabled |
| `automation_role_arn` | The role a run outside the schedule passes as `AutomationAssumeRole`. Null when the toggle is off |
| `function_role_arn` | The role the off-hours function runs as, which reads the monitor secret. Null when the toggle is off |
| `function_name` | The off-hours Lambda function. Null when disabled |
| `wake_counts_parameter` | The parameter holding the counts the wake restores |
| `alert_topic_arn` | The topic a failed check or run publishes to |
| `metric_namespace` | The CloudWatch namespace of the run and check metrics |

## Tests

`tests/off_hours.tftest.hcl` runs the module against a mocked provider and asserts that the disabled module creates nothing, that the schedules sleep nightly and wake on weekdays in the time zone, that the foundation services stop last and start first, that the sleep scales the EKS deployments down and checks before and after the foundation services, that the wake awaits the Temporal server's health before it starts the services and the deployments, that each failed step records and alerts, that the function role reaches only the deployer, its log group, the cluster's Data API, the monitor secret, the topic, and the namespace, that the automation role may only invoke the function, that every step invokes the function with timeouts that outlast it and each document stays under 64 KB, and that a weekend wake is refused only while the rotation runs on a Sunday. `tests/unit/test_off_hours_scale.py` runs `functions/scale.py` against fake ECS, CloudWatch Logs, Data API, SNS, CloudWatch, and SSM clients, and `functions/invoke.py` against a fake Lambda client, and `tests/unit/test_eks_deploy.py` runs the `scale-down`, `scale-up`, and `check-stopped` commands against a fake kubectl. Run them with `inv terraform-test` and `inv ci`.
