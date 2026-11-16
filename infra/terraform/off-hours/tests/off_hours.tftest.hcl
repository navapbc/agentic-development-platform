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
      length(aws_scheduler_schedule.scale) == 0 && length(aws_ssm_document.scale) == 0 && output.documents == {} &&
      length(aws_iam_role.automation) == 0 && length(aws_iam_role.scheduler) == 0 && length(aws_ssm_parameter.wake_counts) == 0 &&
      length(aws_sns_topic.alerts) == 0 && length(aws_kms_key.alerts) == 0 && length(aws_kms_alias.alerts) == 0 &&
      length(aws_lambda_function.scale) == 0 && length(aws_iam_role.function) == 0 && length(aws_cloudwatch_log_group.scale) == 0 &&
      output.function_name == null && output.function_role_arn == null &&
      output.alert_topic_arn == null && output.metric_namespace == null
    )
    error_message = "A disabled schedule created a resource."
  }
}

run "the_schedules_run_in_the_time_zone_with_weekends_off" {
  command = apply

  assert {
    condition = (
      aws_scheduler_schedule.scale["sleep"].schedule_expression == "cron(0 20 ? * * *)" &&
      aws_scheduler_schedule.scale["wake"].schedule_expression == "cron(0 7 ? * MON-FRI *)" &&
      alltrue([for schedule in aws_scheduler_schedule.scale : schedule.schedule_expression_timezone == "America/Chicago"]) &&
      jsondecode(aws_scheduler_schedule.scale["wake"].target[0].input).DocumentName == "agentic-platform-off-hours-wake"
    )
    error_message = "The schedules do not sleep nightly and wake on weekdays in America/Chicago."
  }
}

run "the_foundation_stops_last_and_starts_first" {
  command = apply

  assert {
    condition = (
      jsonencode([for step in yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps : step.inputs.InputPayload.Input.Services if step.inputs.InputPayload.Handler == "sleep"]) == jsonencode([var.services, var.foundation_services]) &&
      jsonencode([for step in yamldecode(aws_ssm_document.scale["wake"].content).mainSteps : step.inputs.InputPayload.Input.Services if step.inputs.InputPayload.Handler == "wake"]) == jsonencode([var.foundation_services, var.services]) &&
      yamldecode(aws_ssm_document.scale["wake"].content).mainSteps[0].inputs.InputPayload.Input.WaitSeconds == 540
    )
    error_message = "The foundation services do not stop after the others and start, and settle, before them."
  }
}

run "the_sleep_stops_eks_and_checks_before_and_after_the_foundation" {
  command = apply

  assert {
    condition = (
      [for step in yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps : step.name] == ["StopServices", "ScaleDownDeployments", "CheckBeforeFoundation", "StopFoundationServices", "CheckBeforePause", "RecordSuccess", "RecordFailure"] &&
      yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[1].inputs.InputPayload.Input.Command == "scale-down" &&
      jsonencode(yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[1].inputs.InputPayload.Input.Deployments) == jsonencode(var.eks_deployer.deployments)
    )
    error_message = "The sleep does not scale the EKS deployments down and check before the foundation services stop and again after."
  }

  assert {
    condition = (
      jsonencode(yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[2].inputs.InputPayload.Input.Services) == jsonencode(var.services) &&
      yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[2].inputs.InputPayload.Input.Database.AllowedUsers == ["temporal"] &&
      toset(yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[4].inputs.InputPayload.Input.Services) == toset(concat(var.services, var.foundation_services)) &&
      yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[4].inputs.InputPayload.Input.Database.AllowedUsers == [] &&
      alltrue([for index in [2, 4] : jsonencode(yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[index].inputs.InputPayload.Input.Deployments) == jsonencode(var.eks_deployer.deployments)])
    )
    error_message = "The check before the foundation does not allow only the foundation users, or the check before the pause allows a user or skips a service or deployment."
  }
}

run "the_wake_starts_workers_only_after_temporal_reports_healthy" {
  command = apply

  assert {
    condition = (
      [for step in yamldecode(aws_ssm_document.scale["wake"].content).mainSteps : step.name] == ["StartFoundationServices", "AwaitFoundationHealth", "StartServices", "ScaleUpDeployments", "RecordSuccess", "RecordFailure"] &&
      jsonencode(yamldecode(aws_ssm_document.scale["wake"].content).mainSteps[1].inputs.InputPayload.Input.Services) == jsonencode(var.health_checked_services) &&
      yamldecode(aws_ssm_document.scale["wake"].content).mainSteps[3].inputs.InputPayload.Input.Command == "scale-up"
    )
    error_message = "The wake does not await the Temporal server's health before it starts the services and the EKS deployments."
  }
}

run "a_failed_step_records_and_alerts_and_a_clean_run_ends_at_its_record" {
  command = apply

  assert {
    condition = alltrue(flatten([
      for document in aws_ssm_document.scale : [
        for step in yamldecode(document.content).mainSteps :
        step.onFailure == (startswith(step.name, "Record") ? "Abort" : "step:RecordFailure") && step.isEnd == (step.name == "RecordSuccess")
      ]
    ]))
    error_message = "A failed step does not go to RecordFailure, or RecordSuccess does not end the run."
  }

  assert {
    condition = alltrue([
      for document in aws_ssm_document.scale : alltrue([
        for step in yamldecode(document.content).mainSteps : step.inputs.InputPayload.Input.Result == (step.name == "RecordSuccess" ? "Completed" : "Failed") && step.inputs.InputPayload.Input.Topic == aws_sns_topic.alerts[0].arn
        if step.inputs.InputPayload.Handler == "record"
      ])
    ])
    error_message = "The record steps do not count the result or name the alert topic."
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
      one([for statement in data.aws_iam_policy_document.function[0].statement : statement.resources if statement.sid == "RaiseTheAlert"]) == toset([aws_sns_topic.alerts[0].arn])
    )
    error_message = "The function role reaches beyond the deployer task, its log group, the cluster, the monitor secret, or the alert topic."
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
      [for step in yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps : step.name] == ["StopServices", "CheckBeforeFoundation", "StopFoundationServices", "CheckBeforePause", "RecordSuccess", "RecordFailure"] &&
      yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[1].inputs.InputPayload.Input.Deployments == [] &&
      yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps[1].inputs.InputPayload.Input.Database.ClusterArn == ""
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

run "each_step_invokes_the_function_and_each_document_fits_the_ssm_size_limit" {
  command = apply

  assert {
    condition = alltrue([
      for document in aws_ssm_document.scale : length(document.content) < 65536
    ])
    error_message = "An Automation document exceeds the 64 KB SSM document limit."
  }

  assert {
    condition = alltrue(flatten([
      for document in aws_ssm_document.scale : [
        for step in yamldecode(document.content).mainSteps :
        step.action == "aws:executeScript" && step.inputs.Handler == "invoke" && step.inputs.InputPayload.Function == aws_lambda_function.scale[0].function_name &&
        step.inputs.InputPayload.ReadTimeout >= aws_lambda_function.scale[0].timeout + 30 && step.timeoutSeconds > step.inputs.InputPayload.ReadTimeout
      ]
    ]))
    error_message = "A step does not invoke the off-hours function, or its timeouts do not outlast the function's."
  }

  assert {
    condition = (
      aws_lambda_function.scale[0].handler == "scale.handler" &&
      aws_lambda_function.scale[0].timeout <= 560 && aws_cloudwatch_log_group.scale[0].kms_key_id == aws_kms_key.alerts[0].arn
    )
    error_message = "The function does not dispatch through scale.handler with a timeout that leaves room for a cold start, or its log group is not encrypted."
  }
}

run "the_automation_role_may_only_invoke_the_function" {
  command = apply

  assert {
    condition = (
      length(data.aws_iam_policy_document.automation[0].statement) == 1 &&
      data.aws_iam_policy_document.automation[0].statement[0].actions == toset(["lambda:InvokeFunction"]) &&
      data.aws_iam_policy_document.automation[0].statement[0].resources == toset([aws_lambda_function.scale[0].arn])
    )
    error_message = "The automation role holds a permission beyond invoking the off-hours function."
  }
}
