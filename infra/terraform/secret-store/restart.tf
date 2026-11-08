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

  # A completed rotation, or an operator update that does not stage a pending version.
  secret_changed_patterns = {
    for key, arn in local.secret_arns : key => jsonencode({
      source = ["aws.secretsmanager"]
      detail = {
        eventSource = ["secretsmanager.amazonaws.com"]
        "$or" = [
          {
            eventName           = ["RotationSucceeded"]
            additionalEventData = { SecretId = [arn] }
          },
          {
            eventName         = ["PutSecretValue"]
            responseElements  = { arn = [arn] }
            requestParameters = { versionStages = [{ exists = false }] }
          },
        ]
      }
    })
  }
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
    sid     = "StartRestartAutomation"
    actions = ["ssm:StartAutomationExecution"]
    # SSM authorizes the call against the automation definition, the document, and the
    # execution it creates.
    resources = [
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${local.restart_document_name}:*",
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:document/${local.restart_document_name}",
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-execution/*",
    ]
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

  event_pattern = local.secret_changed_patterns[each.key]

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

# EKS deployments restart through a one-shot task that rolls them, because no AWS API restarts
# a Kubernetes deployment. Each secret has its own rule, because an EventBridge rule takes at
# most five targets and the ECS rule may already hold five.
locals {
  eks_restart_enabled  = local.enabled && var.restart_eks != null
  eks_restart_secrets  = local.eks_restart_enabled ? var.restart_eks.deployments : {}
  eks_restart_document = "${var.name_prefix}-secret-restart-eks"
}

resource "aws_ssm_document" "restart_eks" {
  count = local.eks_restart_enabled ? 1 : 0

  name            = local.eks_restart_document
  document_type   = "Automation"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "0.3"
    description   = "Run the eks-deployer task to roll EKS deployments, so their new pods read the current secret values."
    assumeRole    = "{{ AutomationAssumeRole }}"
    parameters = {
      AutomationAssumeRole = { type = "String", description = "Role the automation assumes." }
      Command              = { type = "StringList", description = "The eks-deployer command, restart followed by the deployments." }
    }
    mainSteps = [
      {
        name   = "RunTheDeployer"
        action = "aws:executeAwsApi"
        inputs = {
          Service        = "ecs"
          Api            = "RunTask"
          cluster        = var.restart_eks.cluster_arn
          taskDefinition = var.restart_eks.task_family
          launchType     = "FARGATE"
          startedBy      = "secret-restart"
          networkConfiguration = {
            awsvpcConfiguration = {
              subnets        = var.restart_eks.subnet_ids
              securityGroups = var.restart_eks.security_group_ids
              assignPublicIp = "DISABLED"
            }
          }
          overrides = {
            containerOverrides = [{ name = var.restart_eks.container, command = "{{ Command }}" }]
          }
        }
      },
    ]
  })

  tags = var.tags
}

data "aws_iam_policy_document" "eks_automation" {
  count = local.eks_restart_enabled ? 1 : 0

  statement {
    sid       = "RunTheDeployer"
    actions   = ["ecs:RunTask"]
    resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${var.restart_eks.task_family}:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.restart_eks.cluster_arn]
    }
  }

  statement {
    sid       = "PassTheDeployerRoles"
    actions   = ["iam:PassRole"]
    resources = var.restart_eks.pass_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_automation" {
  count = local.eks_restart_enabled ? 1 : 0

  name                 = "${var.name_prefix}-secret-restart-eks-automation"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.automation_assume_eks[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "automation_assume_eks" {
  count = local.eks_restart_enabled ? 1 : 0

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

resource "aws_iam_role_policy" "eks_automation" {
  count = local.eks_restart_enabled ? 1 : 0

  name   = "restart-eks"
  role   = aws_iam_role.eks_automation[0].id
  policy = data.aws_iam_policy_document.eks_automation[0].json
}

data "aws_iam_policy_document" "eks_events" {
  count = local.eks_restart_enabled ? 1 : 0

  statement {
    sid     = "StartRestartAutomation"
    actions = ["ssm:StartAutomationExecution"]
    resources = [
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${local.eks_restart_document}:*",
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:document/${local.eks_restart_document}",
      "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-execution/*",
    ]
  }

  statement {
    sid       = "PassAutomationRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.eks_automation[0].arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ssm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_events" {
  count = local.eks_restart_enabled ? 1 : 0

  name                 = "${var.name_prefix}-secret-restart-eks-events"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.events_assume_eks[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "events_assume_eks" {
  count = local.eks_restart_enabled ? 1 : 0

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

resource "aws_iam_role_policy" "eks_events" {
  count = local.eks_restart_enabled ? 1 : 0

  name   = "restart-eks"
  role   = aws_iam_role.eks_events[0].id
  policy = data.aws_iam_policy_document.eks_events[0].json
}

resource "aws_cloudwatch_event_rule" "restart_eks" {
  for_each = local.eks_restart_secrets

  name          = "${var.name_prefix}-secret-${replace(each.key, "_", "-")}-changed-eks"
  description   = "Restarts the EKS deployments that read the ${each.key} secret after a completed rotation or an operator update."
  event_pattern = local.secret_changed_patterns[each.key]

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "restart_eks" {
  for_each = local.eks_restart_secrets

  rule     = aws_cloudwatch_event_rule.restart_eks[each.key].name
  arn      = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${aws_ssm_document.restart_eks[0].name}:$DEFAULT"
  role_arn = aws_iam_role.eks_events[0].arn

  input = jsonencode({
    AutomationAssumeRole = [aws_iam_role.eks_automation[0].arn]
    Command              = concat(["restart"], each.value)
  })
}
