locals {
  role_labels = {
    workflow-worker    = "workflow"
    review-worker      = "review"
    coordination       = "coord"
    repo-intelligence  = "repo-intel"
    repo-index-worker  = "repo-index"
    db-migrate         = "db-migrate"
    schedule-bootstrap = "schedule"
    clickhouse         = "clickhouse"
    clickhouse-migrate = "ch-migrate"
    langfuse-web       = "lf-web"
    langfuse-worker    = "lf-worker"
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

  workload_secret_names = {
    for name, spec in local.all_workloads : name => distinct([
      for reference in concat(values(spec.secrets), values(try(spec.setup_secrets, {}))) : split(":", reference)[0]
    ])
  }
  workload_secret_arns = {
    for name, spec in local.all_workloads : name => distinct(concat(
      spec.database ? [name == "db-migrate" ? var.database_migrator_secret_arn : var.database_app_secret_arn] : [],
      spec.temporal && local.ca_secret != null ? [local.ca_secret] : [],
      [for key in local.workload_secret_names[name] : lookup(local.secret_arn_lookup, key, "")],
    ))
  }
  workload_secret_keys = {
    for name, spec in local.all_workloads : name => compact(distinct(concat(
      spec.database ? [var.secret_kms_key_arn] : [],
      [for key in local.workload_secret_names[name] : lookup(local.secret_kms_keys, key, "")],
      spec.temporal && local.ca_secret != null ? [try(var.temporal.ca_kms_key_arn, "")] : [],
    )))
  }

  workload_execution_policies = {
    for name, spec in local.all_workloads : name => jsonencode({
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
          Resource = [for repository in local.workload_repositories[name] : module.repository[repository].repository_arn]
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
    for name, spec in local.all_workloads : name => jsonencode({
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
        lookup(local.langfuse_task_statements, name, []),
      )
    })
  }
}

resource "aws_iam_role" "workload_execution" {
  for_each = local.all_workloads

  name_prefix        = substr("${var.name_prefix}-${local.role_labels[each.key]}-exec-", 0, 38)
  description        = "Pulls the ${each.key} image, writes its logs, and injects its secrets."
  assume_role_policy = local.workload_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "workload_execution" {
  for_each = local.all_workloads

  name   = "execution"
  role   = aws_iam_role.workload_execution[each.key].id
  policy = local.workload_execution_policies[each.key]
}

resource "aws_iam_role" "workload_task" {
  for_each = local.all_workloads

  name_prefix        = substr("${var.name_prefix}-${local.role_labels[each.key]}-task-", 0, 38)
  description        = "AWS actions the ${each.key} task calls itself."
  assume_role_policy = local.workload_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "workload_task" {
  for_each = {
    for name, spec in local.all_workloads : name => spec
    if length(spec.mounts) > 0 || (name == "review-worker" && var.evidence_bucket_arn != "") || length(lookup(local.langfuse_task_statements, name, [])) > 0
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
  for_each = { for name, spec in local.all_workloads : name => spec if var.enable_execute_command }

  role       = aws_iam_role.workload_task[each.key].name
  policy_arn = var.exec_task_policy_arn
}
