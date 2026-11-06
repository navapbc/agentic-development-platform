locals {
  role_labels = {
    workflow-worker    = "workflow"
    review-worker      = "review"
    coordination       = "coord"
    repo-intelligence  = "repo-intel"
    repo-index-worker  = "repo-index"
    db-migrate         = "db-migrate"
    schedule-bootstrap = "schedule"
  }

  workload_trust = jsonencode({
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

  workload_secret_arns = {
    for name, spec in local.workloads : name => distinct(concat(
      spec.database ? [name == "db-migrate" ? var.database_migrator_secret_arn : var.database_app_secret_arn] : [],
      spec.temporal && local.ca_secret != null ? [local.ca_secret] : [],
      [for variable, key in spec.secrets : lookup(var.secret_arns, key, "")],
    ))
  }
  workload_secret_keys = {
    for name, spec in local.workloads : name => compact(distinct(concat(
      length(local.workload_secret_arns[name]) > 0 ? [var.secret_kms_key_arn] : [],
      spec.temporal && local.ca_secret != null ? [try(var.temporal.ca_kms_key_arn, "")] : [],
    )))
  }

  workload_execution_policies = {
    for name, spec in local.workloads : name => jsonencode({
      Version = "2012-10-17"
      Statement = concat([
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
          Resource = module.repository[spec.image == var.platform_image ? "platform" : "repo-intelligence"].repository_arn
        },
        {
          Sid      = "WriteTheLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "${aws_cloudwatch_log_group.workload[name].arn}:*"
        },
        ], length(local.workload_secret_arns[name]) == 0 ? [] : [
        {
          Sid      = "ReadTheInjectedSecrets"
          Effect   = "Allow"
          Action   = "secretsmanager:GetSecretValue"
          Resource = local.workload_secret_arns[name]
        },
        {
          Sid      = "DecryptTheInjectedSecrets"
          Effect   = "Allow"
          Action   = "kms:Decrypt"
          Resource = local.workload_secret_keys[name]
        },
      ])
    })
  }

  workload_task_policies = {
    for name, spec in local.workloads : name => jsonencode({
      Version = "2012-10-17"
      Statement = concat(
        [for mount in spec.mounts : {
          Sid      = "Mount${title(mount.volume)}"
          Effect   = "Allow"
          Action   = mount.read_only ? ["elasticfilesystem:ClientMount"] : ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
          Resource = aws_efs_file_system.workspace[0].arn
          Condition = {
            StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.workspace[mount.volume].arn }
          }
        }],
        name == "review-worker" && var.evidence_bucket_arn != "" ? [
          {
            Sid      = "WriteTheEvidence"
            Effect   = "Allow"
            Action   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:GetObjectRetention"]
            Resource = "${var.evidence_bucket_arn}/*"
          },
        ] : [],
        name == "review-worker" && var.evidence_bucket_arn != "" && var.evidence_kms_key_arn != "" ? [
          {
            Sid      = "EncryptTheEvidence"
            Effect   = "Allow"
            Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
            Resource = var.evidence_kms_key_arn
          },
        ] : [],
      )
    })
  }
}

resource "aws_iam_role" "workload_execution" {
  for_each = local.workloads

  name_prefix        = substr("${var.name_prefix}-${local.role_labels[each.key]}-exec-", 0, 38)
  description        = "Pulls the ${each.key} image, writes its logs, and injects its secrets."
  assume_role_policy = local.workload_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "workload_execution" {
  for_each = local.workloads

  name   = "execution"
  role   = aws_iam_role.workload_execution[each.key].id
  policy = local.workload_execution_policies[each.key]
}

resource "aws_iam_role" "workload_task" {
  for_each = local.workloads

  name_prefix        = substr("${var.name_prefix}-${local.role_labels[each.key]}-task-", 0, 38)
  description        = "AWS actions the ${each.key} task calls itself."
  assume_role_policy = local.workload_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "workload_task" {
  for_each = {
    for name, spec in local.workloads : name => spec
    if length(spec.mounts) > 0 || (name == "review-worker" && var.evidence_bucket_arn != "")
  }

  name   = "task"
  role   = aws_iam_role.workload_task[each.key].id
  policy = local.workload_task_policies[each.key]
}

resource "aws_iam_role_policy_attachment" "review_bedrock" {
  count = local.services_enabled && var.bedrock_invoke_policy_arn != "" ? 1 : 0

  role       = aws_iam_role.workload_task["review-worker"].name
  policy_arn = var.bedrock_invoke_policy_arn
}

resource "aws_iam_role_policy_attachment" "workload_exec" {
  for_each = var.enable_execute_command ? local.workloads : {}

  role       = aws_iam_role.workload_task[each.key].name
  policy_arn = var.exec_task_policy_arn
}
