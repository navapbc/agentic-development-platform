data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  enabled = var.create_off_hours_schedule && length(var.services) + length(var.foundation_services) > 0
  # The function role does not depend on the service list, so a caller may name it in a policy
  # that the services depend on, such as the key policy of the secrets the services read.
  role_enabled  = var.create_off_hours_schedule
  account_id    = data.aws_caller_identity.current.account_id
  partition     = data.aws_partition.current.partition
  parameter     = "/${var.name_prefix}/off-hours/wake-counts"
  function_name = "${var.name_prefix}-off-hours"
  log_group     = "/aws/lambda/${local.function_name}"
  function_arn  = "arn:${local.partition}:lambda:${var.region}:${local.account_id}:function:${local.function_name}"
  # Each step runs in its own invocation, so the function timeout bounds one step's longest
  # wait, 540 seconds, and not the whole run.
  function_timeout_seconds = 600
  # A step or a schedule delivery that Lambda or the scheduler cannot hand over within this
  # window, such as under a sustained throttle, goes to its failure path instead of running late.
  event_age_seconds = 900

  # Field 5 of a cron() expression is the day of the week.
  wake_days       = upper(try(split(" ", trimsuffix(trimprefix(var.wake_schedule, "cron("), ")"))[4], ""))
  wakes_weekends  = contains(["*", "?"], local.wake_days) || length(regexall("SAT|SUN|1|7", local.wake_days)) > 0
  sunday_rotation = length(regexall("SUN", upper(var.rotation_schedule_expression))) > 0

  service_arns           = distinct(concat(var.foundation_services, var.services))
  metric_namespace       = "${var.name_prefix}/OffHours"
  dead_letter_alarm_name = "${var.name_prefix}-off-hours-dead-letters"
  dead_letter_alarm_arn  = "arn:${local.partition}:cloudwatch:${var.region}:${local.account_id}:alarm:${local.dead_letter_alarm_name}"
  alert_topic_arn        = "arn:${local.partition}:sns:${var.region}:${local.account_id}:${var.name_prefix}-off-hours-alerts"
  eks_enabled            = local.enabled && var.eks_deployer != null
  database_enabled       = local.enabled && var.database != null
  watched_clusters       = distinct(concat(var.cluster_arns, local.eks_enabled ? [var.eks_deployer.cluster_arn] : []))

  deployer = {
    Cluster        = try(var.eks_deployer.cluster_arn, "")
    TaskFamily     = try(var.eks_deployer.task_family, "")
    Container      = try(var.eks_deployer.container, "")
    Subnets        = try(var.eks_deployer.subnet_ids, [])
    SecurityGroups = try(var.eks_deployer.security_group_ids, [])
    LogGroup       = try(var.eks_deployer.log_group_name, "")
  }
  deployments = local.eks_enabled ? var.eks_deployer.deployments : []
  database = {
    ClusterArn = try(var.database.cluster_arn, "")
    SecretArn  = try(var.database.monitor_secret_arn, "")
  }
  check = {
    Namespace           = local.metric_namespace
    Topic               = local.alert_topic_arn
    Run                 = "sleep"
    Deployer            = local.deployer
    Deployments         = local.deployments
    DeployerWaitSeconds = 300
    WaitSeconds         = 120
  }

  sleep_steps = concat(
    [{ name = "StopServices", handler = "sleep", payload = { Services = var.services, Parameter = local.parameter, WaitSeconds = 540 } }],
    local.eks_enabled ? [{ name = "ScaleDownDeployments", handler = "scale_deployments", payload = { Command = "scale-down", Deployments = local.deployments, Deployer = local.deployer, WaitSeconds = 540 } }] : [],
    [
      { name = "CheckBeforeFoundation", handler = "check", payload = merge(local.check, { Check = "BeforeFoundation", Services = var.services, Database = merge(local.database, { AllowedUsers = var.foundation_database_users }) }) },
      { name = "StopFoundationServices", handler = "sleep", payload = { Services = var.foundation_services, Parameter = local.parameter, WaitSeconds = 540 } },
      { name = "CheckBeforePause", handler = "check", payload = merge(local.check, { Check = "BeforePause", Services = local.service_arns, Database = merge(local.database, { AllowedUsers = [] }) }) },
    ],
  )
  wake_steps = concat(
    [{ name = "StartFoundationServices", handler = "wake", payload = { Services = var.foundation_services, Parameter = local.parameter, WaitSeconds = var.foundation_wait_seconds } }],
    length(var.health_checked_services) > 0 ? [{ name = "AwaitFoundationHealth", handler = "await_healthy", payload = { Services = var.health_checked_services, WaitSeconds = 540 } }] : [],
    [{ name = "StartServices", handler = "wake", payload = { Services = var.services, Parameter = local.parameter, WaitSeconds = 0 } }],
    local.eks_enabled ? [{ name = "ScaleUpDeployments", handler = "scale_deployments", payload = { Command = "scale-up", Deployments = local.deployments, Deployer = local.deployer, WaitSeconds = 540 } }] : [],
  )

  # Each run's steps and record settings, packaged with the function as plan.json.
  plans = {
    sleep = { Steps = local.sleep_steps, Record = merge(local.record, { Run = "sleep" }) }
    wake  = { Steps = local.wake_steps, Record = merge(local.record, { Run = "wake" }) }
  }
  record = {
    Namespace = local.metric_namespace
    Topic     = local.alert_topic_arn
    LogGroup  = local.log_group
  }
}

resource "terraform_data" "rotation_guard" {
  count = local.enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = !local.sunday_rotation || !local.wakes_weekends
      error_message = "The secret store rotates on a Sunday, and wake_schedule wakes on a weekend, so a rotation could find the services running and refuse their new connections. Wake on weekdays only, or move the rotation."
    }
  }
}

resource "aws_ssm_parameter" "wake_counts" {
  count = local.enabled ? 1 : 0

  name        = local.parameter
  description = "Desired count of each ECS service the off-hours schedule scaled to zero, which the wake restores."
  type        = "String"
  value       = "{}"

  tags = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_kms_key" "alerts" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the ${var.name_prefix} off-hours alert topic, dead-letter queue, and function log group."
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "EncryptTheDeadLetterAlarm"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:EncryptionContext:aws:sns:topicArn" = local.alert_topic_arn }
        }
      },
      {
        Sid       = "EncryptTheFunctionLogGroup"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:${local.log_group}"
          }
        }
      },
    ]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-off-hours-alerts" })
}

resource "aws_kms_alias" "alerts" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-off-hours-alerts"
  target_key_id = aws_kms_key.alerts[0].id
}

resource "aws_sns_topic" "alerts" {
  count = local.enabled ? 1 : 0

  name              = "${var.name_prefix}-off-hours-alerts"
  kms_master_key_id = aws_kms_key.alerts[0].arn

  tags = var.tags
}

resource "aws_sns_topic_policy" "alerts" {
  count = local.enabled ? 1 : 0

  arn = aws_sns_topic.alerts[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublishTheDeadLetterAlarm"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alerts[0].arn
      Condition = {
        ArnEquals    = { "aws:SourceArn" = local.dead_letter_alarm_arn }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

data "aws_iam_policy_document" "function" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "WriteTheFunctionLog"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.scale[0].arn}:log-stream:*"]
  }

  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  statement {
    sid       = "InvokeTheNextStep"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.function_arn, "${local.function_arn}:*"]
  }

  statement {
    sid       = "ScaleTheServices"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = local.service_arns
  }

  statement {
    sid       = "WatchTheStoppingTasks"
    actions   = ["ecs:ListTasks", "ecs:DescribeTasks"]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = local.watched_clusters
    }
  }

  statement {
    sid       = "RecordTheCounts"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [aws_ssm_parameter.wake_counts[0].arn]
  }

  statement {
    sid       = "RecordTheResults"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.metric_namespace]
    }
  }

  statement {
    sid       = "RaiseTheAlert"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts[0].arn]
  }

  statement {
    sid       = "EncryptTheAlert"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.alerts[0].arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sns.${var.region}.amazonaws.com"]
    }
  }

  dynamic "statement" {
    for_each = local.eks_enabled ? [var.eks_deployer] : []
    content {
      sid       = "RunTheDeployer"
      actions   = ["ecs:RunTask"]
      resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${statement.value.task_family}:*"]

      condition {
        test     = "ArnEquals"
        variable = "ecs:cluster"
        values   = [statement.value.cluster_arn]
      }
    }
  }

  dynamic "statement" {
    for_each = local.eks_enabled ? [var.eks_deployer] : []
    content {
      sid       = "PassTheDeployerRoles"
      actions   = ["iam:PassRole"]
      resources = statement.value.pass_role_arns

      condition {
        test     = "StringEquals"
        variable = "iam:PassedToService"
        values   = ["ecs-tasks.amazonaws.com"]
      }
    }
  }

  dynamic "statement" {
    for_each = local.eks_enabled ? [var.eks_deployer] : []
    content {
      sid       = "ReadTheDeployerLog"
      actions   = ["logs:GetLogEvents"]
      resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:${statement.value.log_group_name}:log-stream:*"]
    }
  }

  dynamic "statement" {
    for_each = local.database_enabled ? [var.database] : []
    content {
      sid       = "ReadTheConnections"
      actions   = ["rds-data:ExecuteStatement"]
      resources = [statement.value.cluster_arn]
    }
  }

  dynamic "statement" {
    for_each = local.database_enabled ? [var.database] : []
    content {
      sid       = "ReadTheMonitorCredential"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value.monitor_secret_arn]
    }
  }
}

resource "aws_iam_role_policy" "function" {
  count = local.enabled ? 1 : 0

  name   = "scale"
  role   = aws_iam_role.function[0].id
  policy = data.aws_iam_policy_document.function[0].json
}

data "aws_iam_policy_document" "function_assume" {
  count = local.role_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "function" {
  count = local.role_enabled ? 1 : 0

  name               = "${var.name_prefix}-off-hours-function"
  assume_role_policy = data.aws_iam_policy_document.function_assume[0].json

  tags = var.tags
}

data "archive_file" "scale" {
  count = local.enabled ? 1 : 0

  type = "zip"
  source {
    filename = "scale.py"
    content  = file("${path.module}/functions/scale.py")
  }
  source {
    filename = "plan.json"
    content  = jsonencode(local.plans)
  }
  output_path      = "${path.root}/.terraform/build/${var.name_prefix}-off-hours.zip"
  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "scale" {
  count = local.enabled ? 1 : 0

  name              = local.log_group
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.alerts[0].arn

  tags = var.tags
}

resource "aws_lambda_function" "scale" {
  count = local.enabled ? 1 : 0

  function_name    = local.function_name
  description      = "Runs the off-hours sleep and wake steps."
  role             = aws_iam_role.function[0].arn
  runtime          = "python3.14"
  handler          = "scale.handler"
  filename         = data.archive_file.scale[0].output_path
  source_code_hash = data.archive_file.scale[0].output_base64sha256
  timeout          = local.function_timeout_seconds
  memory_size      = 256

  logging_config {
    log_format            = "JSON"
    log_group             = aws_cloudwatch_log_group.scale[0].name
    application_log_level = "INFO"
    system_log_level      = "INFO"
  }

  tracing_config {
    mode = "Active"
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy.function]
}

# A step that times out or crashes the runtime returns no result to the handler, so Lambda
# publishes the invocation record to the alert topic. Retries are off because a timed-out step
# would only wait out the same condition again.
resource "aws_lambda_function_event_invoke_config" "scale" {
  count = local.enabled ? 1 : 0

  function_name                = aws_lambda_function.scale[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = local.event_age_seconds

  destination_config {
    on_failure {
      destination = aws_sns_topic.alerts[0].arn
    }
  }
}

resource "aws_sqs_queue" "dead_letters" {
  count = local.enabled ? 1 : 0

  name                              = "${var.name_prefix}-off-hours-dead-letters"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.alerts[0].arn
  kms_data_key_reuse_period_seconds = 300

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  count = local.enabled ? 1 : 0

  alarm_name          = local.dead_letter_alarm_name
  alarm_description   = "The scheduler could not invoke the off-hours function. Each message in ${aws_sqs_queue.dead_letters[0].name} names the schedule and the error."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dead_letters[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]

  tags = var.tags
}

data "aws_iam_policy_document" "scheduler_assume" {
  count = local.enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  count = local.enabled ? 1 : 0

  name               = "${var.name_prefix}-off-hours-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "scheduler" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "InvokeTheFunction"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.scale[0].arn, "${aws_lambda_function.scale[0].arn}:*"]
  }

  statement {
    sid       = "WriteTheDeadLetters"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dead_letters[0].arn]
  }

  statement {
    sid       = "EncryptTheDeadLetters"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.alerts[0].arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sqs.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  count = local.enabled ? 1 : 0

  name   = "invoke-function"
  role   = aws_iam_role.scheduler[0].id
  policy = data.aws_iam_policy_document.scheduler[0].json
}

resource "aws_scheduler_schedule" "scale" {
  for_each = local.enabled ? { sleep = var.sleep_schedule, wake = var.wake_schedule } : {}

  name                         = "${var.name_prefix}-off-hours-${each.key}"
  description                  = each.key == "sleep" ? "Scale the platform services to zero for the night." : "Restore the platform services for the day."
  schedule_expression          = each.value
  schedule_expression_timezone = var.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scale[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
    input = jsonencode({
      Run                  = each.key
      ScheduleArn          = "<aws.scheduler.schedule-arn>"
      ScheduledTime        = "<aws.scheduler.scheduled-time>"
      SchedulerExecutionId = "<aws.scheduler.execution-id>"
      Attempt              = "<aws.scheduler.attempt-number>"
    })

    retry_policy {
      maximum_event_age_in_seconds = local.event_age_seconds
      maximum_retry_attempts       = 3
    }

    dead_letter_config {
      arn = aws_sqs_queue.dead_letters[0].arn
    }
  }
}
