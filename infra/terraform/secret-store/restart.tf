locals {
  restart_enabled = local.enabled && length(var.restart_services) > 0
  restart_targets = local.restart_enabled ? merge([
    for key, services in var.restart_services : {
      for index, service in services : "${key}-${index}" => {
        secret  = key
        service = service
        cluster = split("/", service)[1]
      }
    }
  ]...) : {}
  restart_document_name = "${var.name_prefix}-secret-restart-service"
}

resource "aws_ssm_document" "restart" {
  count = local.restart_enabled ? 1 : 0

  name            = local.restart_document_name
  document_type   = "Automation"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "0.3"
    description   = "Force a new deployment of an ECS service so its new tasks read the current secret values."
    assumeRole    = "{{ AutomationAssumeRole }}"
    parameters = {
      AutomationAssumeRole = { type = "String", description = "Role the automation assumes." }
      Cluster              = { type = "String", description = "ECS cluster name." }
      Service              = { type = "String", description = "ECS service ARN." }
    }
    mainSteps = [
      {
        name   = "ForceNewDeployment"
        action = "aws:executeAwsApi"
        inputs = {
          Service            = "ecs"
          Api                = "UpdateService"
          cluster            = "{{ Cluster }}"
          service            = "{{ Service }}"
          forceNewDeployment = true
        }
      },
    ]
  })

  tags = var.tags
}

data "aws_iam_policy_document" "automation_assume" {
  count = local.restart_enabled ? 1 : 0

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
  count = local.restart_enabled ? 1 : 0

  name                 = "${var.name_prefix}-secret-restart-automation"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.automation_assume[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "automation" {
  count = local.restart_enabled ? 1 : 0

  statement {
    sid       = "RedeployConsumers"
    actions   = ["ecs:UpdateService"]
    resources = distinct([for target in values(local.restart_targets) : target.service])
  }
}

resource "aws_iam_role_policy" "automation" {
  count = local.restart_enabled ? 1 : 0

  name   = "restart"
  role   = aws_iam_role.automation[0].id
  policy = data.aws_iam_policy_document.automation[0].json
}

data "aws_iam_policy_document" "events_assume" {
  count = local.restart_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "events" {
  count = local.restart_enabled ? 1 : 0

  name                 = "${var.name_prefix}-secret-restart-events"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.events_assume[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "events" {
  count = local.restart_enabled ? 1 : 0

  statement {
    sid       = "StartRestartAutomation"
    actions   = ["ssm:StartAutomationExecution"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${local.restart_document_name}:*"]
  }

  statement {
    sid       = "PassAutomationRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.automation[0].arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ssm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "events" {
  count = local.restart_enabled ? 1 : 0

  name   = "restart"
  role   = aws_iam_role.events[0].id
  policy = data.aws_iam_policy_document.events[0].json
}

resource "aws_cloudwatch_event_rule" "restart" {
  for_each = local.restart_enabled ? var.restart_services : {}

  name        = "${var.name_prefix}-secret-${replace(each.key, "_", "-")}-changed"
  description = "Redeploys the consumers of the ${each.key} secret after a completed rotation or an operator update."

  event_pattern = jsonencode({
    source = ["aws.secretsmanager"]
    detail = {
      eventSource = ["secretsmanager.amazonaws.com"]
      "$or" = [
        {
          eventName           = ["RotationSucceeded"]
          additionalEventData = { SecretId = [local.secret_arns[each.key]] }
        },
        {
          eventName         = ["PutSecretValue"]
          responseElements  = { arn = [local.secret_arns[each.key]] }
          requestParameters = { versionStages = [{ exists = false }] }
        },
      ]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "restart" {
  for_each = local.restart_targets

  rule     = aws_cloudwatch_event_rule.restart[each.value.secret].name
  arn      = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${aws_ssm_document.restart[0].name}:$DEFAULT"
  role_arn = aws_iam_role.events[0].arn

  input = jsonencode({
    AutomationAssumeRole = [aws_iam_role.automation[0].arn]
    Cluster              = [each.value.cluster]
    Service              = [each.value.service]
  })
}
