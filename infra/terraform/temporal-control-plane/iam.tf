locals {
  role_labels = { server = "server", admin_tools = "admin", ui = "ui" }
  role_names  = toset(local.deploy ? concat(["server", "admin_tools"], local.ui ? ["ui"] : []) : [])

  roles = local.deploy ? merge(
    {
      server = {
        repository = module.server_repository.repository_arn
        secrets = compact([
          var.database_secret_arn,
          try(aws_secretsmanager_secret.tls["ca"].arn, ""),
          try(aws_secretsmanager_secret.tls["internode"].arn, ""),
          try(aws_secretsmanager_secret.tls["frontend"].arn, ""),
        ])
        log_group = "server"
      }
      admin_tools = {
        repository = module.admin_tools_repository.repository_arn
        secrets    = compact([var.database_secret_arn, local.client_ca ? aws_secretsmanager_secret.tls["ca"].arn : ""])
        log_group  = "admin-tools"
      }
    },
    local.ui ? {
      ui = {
        repository = module.ui_repository.repository_arn
        secrets    = compact([local.client_ca ? aws_secretsmanager_secret.tls["ca"].arn : ""])
        log_group  = "ui"
      }
    } : {},
  ) : {}

  task_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ecs:${local.region}:${local.account_id}:*" }
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
    ]
  })

  execution_policies = {
    for name, role in local.roles : name => jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid      = "AuthenticateToEcr"
          Effect   = "Allow"
          Action   = "ecr:GetAuthorizationToken"
          Resource = "*"
        },
        {
          Sid      = "PullTheImage"
          Effect   = "Allow"
          Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
          Resource = role.repository
        },
        {
          Sid      = "WriteTheLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${aws_cloudwatch_log_group.task[role.log_group].arn}:*"
        },
        {
          Sid      = "ReadTheInjectedSecrets"
          Effect   = "Allow"
          Action   = "secretsmanager:GetSecretValue"
          Resource = role.secrets
        },
        {
          Sid      = "DecryptTheInjectedSecrets"
          Effect   = "Allow"
          Action   = "kms:Decrypt"
          Resource = distinct([aws_kms_key.temporal[0].arn, var.database_secret_kms_key_arn])
        },
      ]
    })
  }
}

resource "aws_iam_role" "execution" {
  for_each = local.role_names

  name_prefix        = substr("${var.name_prefix}-temporal-${local.role_labels[each.key]}-exec-", 0, 38)
  description        = "Pulls the Temporal ${replace(each.key, "_", " ")} image and injects its secrets."
  assume_role_policy = local.task_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.role_names

  name   = "execution"
  role   = aws_iam_role.execution[each.key].id
  policy = local.execution_policies[each.key]
}

resource "aws_iam_role" "task" {
  for_each = var.enable_execute_command ? local.role_names : toset([])

  name_prefix        = substr("${var.name_prefix}-temporal-${local.role_labels[each.key]}-task-", 0, 38)
  description        = "Accepts ECS Exec sessions into the Temporal ${replace(each.key, "_", " ")} task. The task calls no other AWS API."
  assume_role_policy = local.task_trust

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "task_exec" {
  for_each = var.enable_execute_command ? local.role_names : toset([])

  role       = aws_iam_role.task[each.key].name
  policy_arn = var.exec_task_policy_arn
}
