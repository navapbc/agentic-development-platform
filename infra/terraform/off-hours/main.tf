data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  enabled    = var.create_off_hours_schedule && length(var.services) + length(var.foundation_services) > 0
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  parameter  = "/${var.name_prefix}/off-hours/wake-counts"
  script     = file("${path.module}/functions/scale.py")

  # Field 5 of a cron() expression is the day of the week.
  wake_days       = upper(try(split(" ", trimsuffix(trimprefix(var.wake_schedule, "cron("), ")"))[4], ""))
  wakes_weekends  = contains(["*", "?"], local.wake_days) || length(regexall("SAT|SUN|1|7", local.wake_days)) > 0
  sunday_rotation = length(regexall("SUN", upper(var.rotation_schedule_expression))) > 0

  documents = {
    sleep = {
      description = "Record the desired count of each running ECS service and scale it to zero, the services before the foundation services."
      steps = [
        { name = "StopServices", handler = "sleep", services = var.services, wait = 540 },
        { name = "StopFoundationServices", handler = "sleep", services = var.foundation_services, wait = 0 },
      ]
    }
    wake = {
      description = "Restore the recorded desired count of each ECS service, the foundation services first."
      steps = [
        { name = "StartFoundationServices", handler = "wake", services = var.foundation_services, wait = var.foundation_wait_seconds },
        { name = "StartServices", handler = "wake", services = var.services, wait = 0 },
      ]
    }
  }
  service_arns = distinct(concat(var.foundation_services, var.services))
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

resource "aws_ssm_document" "scale" {
  for_each = local.enabled ? local.documents : {}

  name            = "${var.name_prefix}-off-hours-${each.key}"
  document_type   = "Automation"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "0.3"
    description   = each.value.description
    assumeRole    = "{{ AutomationAssumeRole }}"
    parameters = {
      AutomationAssumeRole = { type = "String", description = "Role the automation assumes." }
    }
    mainSteps = [
      for step in each.value.steps : {
        name           = step.name
        action         = "aws:executeScript"
        timeoutSeconds = 600
        inputs = {
          Runtime      = "python3.11"
          Handler      = step.handler
          Script       = local.script
          InputPayload = { Services = step.services, Parameter = local.parameter, WaitSeconds = step.wait }
        }
      }
    ]
  })

  tags = var.tags
}

data "aws_iam_policy_document" "automation_assume" {
  count = local.enabled ? 1 : 0

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
  count = local.enabled ? 1 : 0

  name               = "${var.name_prefix}-off-hours-automation"
  assume_role_policy = data.aws_iam_policy_document.automation_assume[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "automation" {
  count = local.enabled ? 1 : 0

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
      values   = var.cluster_arns
    }
  }

  statement {
    sid       = "RecordTheCounts"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [aws_ssm_parameter.wake_counts[0].arn]
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
