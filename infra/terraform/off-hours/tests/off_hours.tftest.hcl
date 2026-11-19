mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/agentic-platform-off-hours" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:us-east-1:111111111111:parameter/agentic-platform/off-hours/wake-counts" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:111111111111:agentic-platform-off-hours-alerts" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:111111111111:log-group:/aws/lambda/agentic-platform-off-hours" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:111111111111:agentic-platform-off-hours-dead-letters"
      url = "https://sqs.us-east-1.amazonaws.com/111111111111/agentic-platform-off-hours-dead-letters"
    }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-off-hours" }
  }
}

variables {
  create_off_hours_schedule = true
  name_prefix               = "agentic-platform"
  region                    = "us-east-1"
  cluster_arns              = ["arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"]
  foundation_services       = ["arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-temporal-server"]
  health_checked_services   = ["arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-temporal-server"]
  foundation_database_users = ["temporal"]
  eks_deployer = {
    cluster_arn        = "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
    task_family        = "agentic-platform-eks-deployer"
    container          = "eks-deployer"
    subnet_ids         = ["subnet-management"]
    security_group_ids = ["sg-deployer"]
    pass_role_arns     = ["arn:aws:iam::111111111111:role/deployer-exec", "arn:aws:iam::111111111111:role/deployer-task"]
    log_group_name     = "/aws/ecs/agentic-platform-eks-deployer"
    deployments        = ["tenant-workers", "mcp-gateway", "inbound-webhook", "litellm"]
  }
  database = {
    cluster_arn        = "arn:aws:rds:us-east-1:111111111111:cluster:agentic-platform-aurora"
    monitor_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/monitor-AbCdEf"
  }
  services = [
    "arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-coordination",
    "arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-workflow-worker-blue",
  ]
}

run "disabled_creates_nothing" {
  command = plan

  variables {
    create_off_hours_schedule = false
  }

  assert {
    condition = (
      length(aws_scheduler_schedule.scale) == 0 && length(aws_iam_role.scheduler) == 0 && length(aws_ssm_parameter.wake_counts) == 0 &&
      length(aws_sns_topic.alerts) == 0 && length(aws_kms_key.alerts) == 0 && length(aws_kms_alias.alerts) == 0 &&
      length(aws_lambda_function.scale) == 0 && length(aws_iam_role.function) == 0 && length(aws_cloudwatch_log_group.scale) == 0 &&
      length(aws_lambda_function_event_invoke_config.scale) == 0 && length(aws_sqs_queue.dead_letters) == 0 &&
      length(aws_cloudwatch_metric_alarm.dead_letters) == 0 && length(aws_sns_topic_policy.alerts) == 0 &&
      output.function_name == null && output.function_arn == null && output.function_role_arn == null && output.dead_letter_queue_url == null &&
      output.alert_topic_arn == null && output.metric_namespace == null
    )
    error_message = "A disabled schedule created a resource."
  }
}

run "the_schedules_invoke_the_function_in_the_time_zone_with_weekends_off" {
  command = apply

  assert {
    condition = (
      aws_scheduler_schedule.scale["sleep"].schedule_expression == "cron(0 20 ? * * *)" &&
      aws_scheduler_schedule.scale["wake"].schedule_expression == "cron(0 7 ? * MON-FRI *)" &&
      alltrue([for schedule in aws_scheduler_schedule.scale : schedule.schedule_expression_timezone == "America/Chicago"]) &&
      alltrue([for run, schedule in aws_scheduler_schedule.scale : schedule.target[0].arn == aws_lambda_function.scale[0].arn && jsondecode(schedule.target[0].input).Run == run])
    )
    error_message = "The schedules do not invoke the off-hours function to sleep nightly and wake on weekdays in America/Chicago."
  }

  assert {
    condition = alltrue([
      for schedule in aws_scheduler_schedule.scale : jsondecode(schedule.target[0].input) == {
        Run                  = jsondecode(schedule.target[0].input).Run
        ScheduleArn          = "<aws.scheduler.schedule-arn>"
        ScheduledTime        = "<aws.scheduler.scheduled-time>"
        SchedulerExecutionId = "<aws.scheduler.execution-id>"
        Attempt              = "<aws.scheduler.attempt-number>"
      }
    ])
    error_message = "A schedule does not pass the schedule, the scheduled time, the execution, and the attempt that the audit records name."
  }
}

run "the_foundation_stops_last_and_starts_first" {
  command = apply

  assert {
    condition = (
      jsonencode([for step in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps : step.payload.Services if step.handler == "sleep"]) == jsonencode([var.services, var.foundation_services]) &&
      jsonencode([for step in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).wake.Steps : step.payload.Services if step.handler == "wake"]) == jsonencode([var.foundation_services, var.services]) &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).wake.Steps[0].payload.WaitSeconds == 540
    )
    error_message = "The foundation services do not stop after the others and start, and settle, before them."
  }
}

run "the_sleep_stops_eks_and_checks_before_and_after_the_foundation" {
  command = apply

  assert {
    condition = (
      [for step in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps : step.name] == ["StopServices", "ScaleDownDeployments", "CheckBeforeFoundation", "StopFoundationServices", "CheckBeforePause"] &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[1].payload.Command == "scale-down" &&
      jsonencode(one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[1].payload.Deployments) == jsonencode(var.eks_deployer.deployments)
    )
    error_message = "The sleep does not scale the EKS deployments down and check before the foundation services stop and again after."
  }

  assert {
    condition = (
      jsonencode(one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[2].payload.Services) == jsonencode(var.services) &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[2].payload.Database.AllowedUsers == ["temporal"] &&
      toset(one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[4].payload.Services) == toset(concat(var.services, var.foundation_services)) &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[4].payload.Database.AllowedUsers == [] &&
      alltrue([for index in [2, 4] : jsonencode(one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[index].payload.Deployments) == jsonencode(var.eks_deployer.deployments)])
    )
    error_message = "The check before the foundation does not allow only the foundation users, or the check before the pause allows a user or skips a service or deployment."
  }
}

run "the_wake_starts_workers_only_after_temporal_reports_healthy" {
  command = apply

  assert {
    condition = (
      [for step in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).wake.Steps : step.name] == ["StartFoundationServices", "AwaitFoundationHealth", "StartServices", "ScaleUpDeployments"] &&
      jsonencode(one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).wake.Steps[1].payload.Services) == jsonencode(var.health_checked_services) &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).wake.Steps[3].payload.Command == "scale-up"
    )
    error_message = "The wake does not await the Temporal server's health before it starts the services and the EKS deployments."
  }
}

run "each_run_records_its_result_to_the_namespace_the_topic_and_the_log_group" {
  command = apply

  assert {
    condition = alltrue([
      for run, plan in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]) : plan.Record == {
        Run       = run
        Namespace = "agentic-platform/OffHours"
        Topic     = aws_sns_topic.alerts[0].arn
        LogGroup  = aws_cloudwatch_log_group.scale[0].name
      }
    ])
    error_message = "A run does not record its result to the off-hours namespace and alert topic, or does not name the log group of its audit records."
  }
}

run "the_function_role_is_scoped_to_the_deployer_the_cluster_and_the_monitor_secret" {
  command = apply

  assert {
    condition = (
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "RunTheDeployer"]) == toset(["arn:aws:ecs:us-east-1:111111111111:task-definition/agentic-platform-eks-deployer:*"]) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "PassTheDeployerRoles"]) == toset(var.eks_deployer.pass_role_arns) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "ReadTheDeployerLog"]) == toset(["arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-eks-deployer:log-stream:*"]) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "ReadTheConnections"]) == toset([var.database.cluster_arn]) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "ReadTheMonitorCredential"]) == toset([var.database.monitor_secret_arn]) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "RaiseTheAlert"]) == toset([aws_sns_topic.alerts[0].arn]) &&
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "InvokeTheNextStep"]) == toset([aws_lambda_function.scale[0].arn, "${aws_lambda_function.scale[0].arn}:*"])
    )
    error_message = "The function role reaches beyond the deployer task, its log group, the cluster, the monitor secret, the alert topic, or the function itself."
  }

  assert {
    condition = (
      one(one([for statement in data.aws_iam_policy_document.function[0].statement : statement.condition if statement.sid == "RecordTheResults"])).values == tolist(["agentic-platform/OffHours"]) &&
      one(one([for statement in data.aws_iam_policy_document.function[0].statement : statement.condition if statement.sid == "RunTheDeployer"])).values == tolist([var.eks_deployer.cluster_arn])
    )
    error_message = "The metrics are not limited to the off-hours namespace, or the deployer is not limited to its cluster."
  }
}

run "without_eks_or_a_database_the_function_neither_runs_the_deployer_nor_reads_the_cluster" {
  command = apply

  variables {
    eks_deployer = null
    database     = null
  }

  assert {
    condition = (
      length([for statement in data.aws_iam_policy_document.function[0].statement : statement if contains(["RunTheDeployer", "PassTheDeployerRoles", "ReadTheDeployerLog", "ReadTheConnections", "ReadTheMonitorCredential"], statement.sid)]) == 0 &&
      [for step in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps : step.name] == ["StopServices", "CheckBeforeFoundation", "StopFoundationServices", "CheckBeforePause"] &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[1].payload.Deployments == [] &&
      one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]).sleep.Steps[1].payload.Database.ClusterArn == ""
    )
    error_message = "Without EKS or a database, the function still runs the deployer or reads the cluster."
  }
}

run "a_health_checked_service_outside_the_foundation_is_refused" {
  command = plan

  variables {
    health_checked_services = ["arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-coordination"]
  }

  expect_failures = [var.health_checked_services]
}

run "a_weekend_wake_with_a_sunday_rotation_is_refused" {
  command = plan

  variables {
    wake_schedule = "cron(0 7 ? * * *)"
  }

  expect_failures = [terraform_data.rotation_guard]
}

run "a_weekend_wake_is_allowed_when_the_rotation_is_not_on_a_sunday" {
  command = plan

  variables {
    wake_schedule                = "cron(0 7 ? * * *)"
    rotation_schedule_expression = "cron(0 6 ? 1/3 WED#1 *)"
  }

  assert {
    condition     = length(terraform_data.rotation_guard) == 1
    error_message = "A weekend wake was refused although the rotation does not run on a Sunday."
  }
}

run "each_step_finishes_its_longest_wait_within_the_function_timeout" {
  command = apply

  assert {
    condition = (
      aws_lambda_function.scale[0].handler == "scale.handler" && aws_lambda_function.scale[0].runtime == "python3.14" &&
      alltrue(flatten([for plan in one([for file in data.archive_file.scale[0].source : jsondecode(file.content) if file.filename == "plan.json"]) : [for step in plan.Steps : try(step.payload.WaitSeconds, 0) + 30 < aws_lambda_function.scale[0].timeout]]))
    )
    error_message = "The function does not run scale.handler on python3.14, or a step's wait does not finish within the function timeout."
  }
}

run "the_function_writes_encrypted_json_audit_logs" {
  command = apply

  assert {
    condition = (
      aws_lambda_function.scale[0].logging_config[0].log_format == "JSON" &&
      aws_lambda_function.scale[0].logging_config[0].log_group == aws_cloudwatch_log_group.scale[0].name &&
      aws_lambda_function.scale[0].logging_config[0].application_log_level == "INFO" &&
      aws_lambda_function.scale[0].logging_config[0].system_log_level == "INFO" &&
      aws_lambda_function.scale[0].tracing_config[0].mode == "Active" &&
      aws_cloudwatch_log_group.scale[0].kms_key_id == aws_kms_key.alerts[0].arn &&
      aws_cloudwatch_log_group.scale[0].retention_in_days == 365
    )
    error_message = "The function does not write JSON records at INFO to its KMS-encrypted log group with a year of retention, or does not trace."
  }
}

run "a_step_that_times_out_or_crashes_alerts_without_a_retry" {
  command = apply

  assert {
    condition = (
      aws_lambda_function_event_invoke_config.scale[0].function_name == aws_lambda_function.scale[0].function_name &&
      aws_lambda_function_event_invoke_config.scale[0].maximum_retry_attempts == 0 &&
      aws_lambda_function_event_invoke_config.scale[0].maximum_event_age_in_seconds == 900 &&
      aws_lambda_function_event_invoke_config.scale[0].destination_config[0].on_failure[0].destination == aws_sns_topic.alerts[0].arn
    )
    error_message = "A step that the function cannot finish is retried, or does not publish its invocation record to the alert topic."
  }
}

run "a_schedule_the_scheduler_cannot_deliver_alerts_from_the_dead_letter_queue" {
  command = apply

  assert {
    condition = alltrue([
      for schedule in aws_scheduler_schedule.scale :
      schedule.target[0].dead_letter_config[0].arn == aws_sqs_queue.dead_letters[0].arn &&
      schedule.target[0].retry_policy[0].maximum_event_age_in_seconds == 900 &&
      schedule.target[0].retry_policy[0].maximum_retry_attempts == 3
    ])
    error_message = "A schedule does not retry a failed delivery and then keep it in the dead-letter queue."
  }

  assert {
    condition = (
      aws_sqs_queue.dead_letters[0].kms_master_key_id == aws_kms_key.alerts[0].arn &&
      aws_cloudwatch_metric_alarm.dead_letters[0].namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.dead_letters[0].metric_name == "ApproximateNumberOfMessagesVisible" &&
      aws_cloudwatch_metric_alarm.dead_letters[0].dimensions == tomap({ QueueName = aws_sqs_queue.dead_letters[0].name }) &&
      aws_cloudwatch_metric_alarm.dead_letters[0].threshold == 0 &&
      aws_cloudwatch_metric_alarm.dead_letters[0].comparison_operator == "GreaterThanThreshold" &&
      aws_cloudwatch_metric_alarm.dead_letters[0].alarm_actions == toset([aws_sns_topic.alerts[0].arn])
    )
    error_message = "The dead-letter queue is not encrypted with the alert key, or a message in it does not alarm to the alert topic."
  }

  assert {
    condition = (
      one(jsondecode(aws_sns_topic_policy.alerts[0].policy).Statement).Principal.Service == "cloudwatch.amazonaws.com" &&
      one(jsondecode(aws_sns_topic_policy.alerts[0].policy).Statement).Condition.ArnEquals["aws:SourceArn"] == "arn:aws:cloudwatch:us-east-1:111111111111:alarm:${aws_cloudwatch_metric_alarm.dead_letters[0].alarm_name}" &&
      one([for statement in jsondecode(aws_kms_key.alerts[0].policy).Statement : statement.Principal.Service if statement.Sid == "EncryptTheDeadLetterAlarm"]) == "cloudwatch.amazonaws.com" &&
      one([for statement in jsondecode(aws_kms_key.alerts[0].policy).Statement : statement.Condition.StringEquals["kms:EncryptionContext:aws:sns:topicArn"] if statement.Sid == "EncryptTheDeadLetterAlarm"]) == "arn:aws:sns:us-east-1:111111111111:agentic-platform-off-hours-alerts"
    )
    error_message = "The alert topic does not admit only the dead-letter alarm, or its key does not let CloudWatch encrypt for that topic alone."
  }
}

run "the_scheduler_role_may_only_invoke_the_function_and_write_its_dead_letters" {
  command = apply

  assert {
    condition = (
      toset([for statement in data.aws_iam_policy_document.scheduler[0].statement : statement.sid]) == toset(["InvokeTheFunction", "WriteTheDeadLetters", "EncryptTheDeadLetters"]) &&
      one([for statement in data.aws_iam_policy_document.scheduler[0].statement : statement.actions if statement.sid == "InvokeTheFunction"]) == toset(["lambda:InvokeFunction"]) &&
      one([for statement in data.aws_iam_policy_document.scheduler[0].statement : statement.resources if statement.sid == "InvokeTheFunction"]) == toset([aws_lambda_function.scale[0].arn, "${aws_lambda_function.scale[0].arn}:*"]) &&
      one([for statement in data.aws_iam_policy_document.scheduler[0].statement : statement.resources if statement.sid == "WriteTheDeadLetters"]) == toset([aws_sqs_queue.dead_letters[0].arn]) &&
      one(one([for statement in data.aws_iam_policy_document.scheduler[0].statement : statement.condition if statement.sid == "EncryptTheDeadLetters"])).values == tolist(["sqs.us-east-1.amazonaws.com"])
    )
    error_message = "The scheduler role holds a permission beyond invoking the off-hours function and writing its dead-letter queue."
  }
}

run "the_function_arn_is_known_at_plan_for_the_trail_selector" {
  command = plan

  assert {
    condition     = output.function_arn == "arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-off-hours"
    error_message = "The function ARN is not known at plan, so the trail cannot select its data events on the first apply."
  }
}
