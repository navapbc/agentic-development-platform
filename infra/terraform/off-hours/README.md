# off-hours

Module that scales ECS services to zero overnight and restores them in the morning, so the Aurora cluster that the services hold connections to auto-pauses and the Fargate tasks stop billing. Nothing is created unless `create_off_hours_schedule = true` and at least one service is listed.

## What it provisions

- Two EventBridge Scheduler schedules, `<name_prefix>-off-hours-sleep` on `sleep_schedule` and `<name_prefix>-off-hours-wake` on `wake_schedule`, both in `timezone`. Each starts an SSM Automation document.
- The `<name_prefix>-off-hours-sleep` and `<name_prefix>-off-hours-wake` Automation documents, which run `functions/scale.py` through `aws:executeScript`.
- The `/<name_prefix>/off-hours/wake-counts` SSM parameter, which holds the desired count of each service the sleep scaled to zero. Terraform creates it as `{}` and ignores its value afterwards.
- An automation role that may only describe and update the listed services, list and describe the tasks of `cluster_arns`, and read and write the parameter, and a scheduler role that may only start the two documents and pass the automation role to SSM.

## Order

`foundation_services` are the services the others depend on, such as the Temporal server and UI, ClickHouse, and the collector. The sleep scales `services` to zero first and waits up to 540 seconds until every one of their tasks has reached `DEPROVISIONING` or `STOPPED`, then scales `foundation_services` to zero. ECS lowers a service's running count as soon as a task starts to stop, while its containers keep running through the load balancer drain and the stop timeout, so the wait reads the tasks rather than the count. The wake restores `foundation_services`, waits up to `foundation_wait_seconds` for their tasks to run, then restores `services`.

The sleep records only the services that run tasks, and the wake restores only the recorded services and removes them from the record. A service at zero when the sleep runs, such as a drained Temporal worker slot, therefore stays at zero after the wake, and a second sleep keeps the counts the first recorded. A deployment during the night starts its services as usual, and the next sleep records and stops them again.

## Credential rotation

The secret store rotates a single database or cache user, so a rotation refuses the new connections of a service that is still running until its restart rule replaces its tasks. The secret store rotates on the first Sunday of every third month between 06:00 and 09:00 UTC by default, which falls inside the default window of sleep at 20:00 daily and wake at 07:00 Monday through Friday, America/Chicago. A rotation then finds every service at zero, the restart rule redeploys each one at zero tasks, and the wake starts it with the new password. `rotation_schedule_expression` takes the secret store's schedule, and the module refuses a `wake_schedule` that wakes on a Saturday or a Sunday while that schedule rotates on a Sunday.

## Running it outside the schedule

Start either document with the automation role from the outputs, for example `aws ssm start-automation-execution --document-name agentic-platform-off-hours-wake --parameters AutomationAssumeRole=<automation_role_arn>`, to wake the services early or put them to sleep before a manual rotation.

## Outputs

| Output | Value |
|---|---|
| `documents` | The sleep and wake document names. Empty when disabled |
| `automation_role_arn` | The role a run outside the schedule passes as `AutomationAssumeRole` |
| `wake_counts_parameter` | The parameter holding the counts the wake restores |

## Tests

`tests/off_hours.tftest.hcl` runs the module against a mocked provider and asserts that the disabled module creates nothing, that the schedules sleep nightly and wake on weekdays in the time zone, that the foundation services stop last and start first, and that a weekend wake is refused only while the rotation runs on a Sunday. `tests/unit/test_off_hours_scale.py` runs `functions/scale.py` against fake ECS and SSM clients. Run them with `inv terraform-test` and `inv ci`.
