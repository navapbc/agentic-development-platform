data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  enabled = var.create_off_hours_schedule && length(var.services) + length(var.foundation_services) > 0
  # The automation and function roles do not depend on the service list, so a caller may name
  # them in a policy that the services depend on, such as the key policy of the secrets the
  # services read.
  role_enabled  = var.create_off_hours_schedule
  account_id    = data.aws_caller_identity.current.account_id
  partition     = data.aws_partition.current.partition
  parameter     = "/${var.name_prefix}/off-hours/wake-counts"
  function_name = "${var.name_prefix}-off-hours"
  log_group     = "/aws/lambda/${local.function_name}"
  # SSM caps a document at 64 KB and an aws:invokeLambdaFunction step at 300 seconds, shorter
  # than the waits below. Each step therefore runs invoke.py through aws:executeScript, which
  # allows 600 seconds, and the handlers run in the function. The read timeout exceeds the
  # function timeout by more than a cold start's init phase, which the function timeout does
  # not count.
  invoke_script            = file("${path.module}/functions/invoke.py")
  step_timeout_seconds     = 600
  read_timeout_seconds     = 590
  function_timeout_seconds = 560
  throttle_seconds         = 60

  # Field 5 of a cron() expression is the day of the week.
  wake_days       = upper(try(split(" ", trimsuffix(trimprefix(var.wake_schedule, "cron("), ")"))[4], ""))
  wakes_weekends  = contains(["*", "?"], local.wake_days) || length(regexall("SAT|SUN|1|7", local.wake_days)) > 0
  sunday_rotation = length(regexall("SUN", upper(var.rotation_schedule_expression))) > 0

  service_arns     = distinct(concat(var.foundation_services, var.services))
  metric_namespace = "${var.name_prefix}/OffHours"
  topic_arn        = try(aws_sns_topic.alerts[0].arn, "")
  eks_enabled      = local.enabled && var.eks_deployer != null
  database_enabled = local.enabled && var.database != null
  watched_clusters = distinct(concat(var.cluster_arns, local.eks_enabled ? [var.eks_deployer.cluster_arn] : []))

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
    Topic               = local.topic_arn
    Document            = "sleep"
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

  documents = {
    sleep = {
      description = "Scale the ECS services and the EKS deployments that connect to the database to zero, check that none runs and that only the foundation services hold a connection, scale the foundation services to zero, and check that nothing holds a connection."
      steps       = local.sleep_steps
    }
    wake = {
      description = "Restore the foundation ECS services, wait until the Temporal server reports healthy, then restore the other ECS services and the EKS deployments."
      steps       = local.wake_steps
    }
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

  description         = "Encrypts the ${var.name_prefix} off-hours alert topic and function log group."
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

resource "aws_ssm_document" "scale" {
  for_each = toset(local.enabled ? keys(local.documents) : [])

  name            = "${var.name_prefix}-off-hours-${each.key}"
  document_type   = "Automation"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "0.3"
    description   = local.documents[each.key].description
    assumeRole    = "{{ AutomationAssumeRole }}"
    parameters = {
      AutomationAssumeRole = { type = "String", description = "Role the automation assumes." }
    }
    mainSteps = [
      for step in concat(local.documents[each.key].steps, [
        { name = "RecordSuccess", handler = "record", payload = { Result = "Completed", Document = each.key, Namespace = local.metric_namespace, Topic = local.topic_arn, ExecutionId = "{{ automation:EXECUTION_ID }}" } },
        { name = "RecordFailure", handler = "record", payload = { Result = "Failed", Document = each.key, Namespace = local.metric_namespace, Topic = local.topic_arn, ExecutionId = "{{ automation:EXECUTION_ID }}" } },
        ]) : {
        name           = step.name
        action         = "aws:executeScript"
        timeoutSeconds = local.step_timeout_seconds
        onFailure      = startswith(step.name, "Record") ? "Abort" : "step:RecordFailure"
        isEnd          = step.name == "RecordSuccess"
        inputs = {
          Runtime = "python3.11"
          Handler = "invoke"
          Script  = local.invoke_script
          InputPayload = {
            Function        = aws_lambda_function.scale[0].function_name
            Handler         = step.handler
            Input           = step.payload
            ReadTimeout     = local.read_timeout_seconds
            ThrottleSeconds = local.throttle_seconds
          }
        }
      }
    ]
  })

  tags = var.tags
}

data "aws_iam_policy_document" "automation_assume" {
  count = local.role_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ssm.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "automation" {
  count = local.role_enabled ? 1 : 0

  name               = "${var.name_prefix}-off-hours-automation"
  assume_role_policy = data.aws_iam_policy_document.automation_assume[0].json

  tags = var.tags
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

  type             = "zip"
  source_file      = "${path.module}/functions/scale.py"
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
  runtime          = "python3.13"
  handler          = "scale.handler"
  filename         = data.archive_file.scale[0].output_path
  source_code_hash = data.archive_file.scale[0].output_base64sha256
  timeout          = local.function_timeout_seconds
  memory_size      = 256

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.scale[0].name
  }

  tracing_config {
    mode = "Active"
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy.function]
}

data "aws_iam_policy_document" "automation" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "InvokeTheFunction"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.scale[0].arn]
  }
}

resource "aws_iam_role_policy" "automation" {
  count = local.enabled ? 1 : 0

  name   = "scale"
  role   = aws_iam_role.automation[0].id
  policy = data.aws_iam_policy_document.automation[0].json
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
    sid     = "StartTheAutomation"
    actions = ["ssm:StartAutomationExecution"]
    # SSM authorizes the call against the automation definition, the document, and the
    # execution it creates.
    resources = flatten([
      for document in aws_ssm_document.scale : [
        "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${document.name}:*",
        "arn:${local.partition}:ssm:${var.region}:${local.account_id}:document/${document.name}",
      ]
    ])
  }

  statement {
    sid       = "TrackTheExecution"
    actions   = ["ssm:StartAutomationExecution"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-execution/*"]
  }

  statement {
    sid       = "PassTheAutomationRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.automation[0].arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ssm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  count = local.enabled ? 1 : 0

  name   = "start-automation"
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
    arn      = "arn:${local.partition}:scheduler:::aws-sdk:ssm:startAutomationExecution"
    role_arn = aws_iam_role.scheduler[0].arn
    input = jsonencode({
      DocumentName = aws_ssm_document.scale[each.key].name
      Parameters   = { AutomationAssumeRole = [aws_iam_role.automation[0].arn] }
    })
  }
}
