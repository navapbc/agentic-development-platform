data "aws_iam_policy_document" "ecs_tasks_assume" {
  count = local.enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  count = local.enabled ? 1 : 0

  name                 = "${local.task_label}-exec"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume[0].json

  tags = var.tags
}

resource "aws_iam_role" "task" {
  count = local.enabled ? 1 : 0

  name                 = "${local.task_label}-task"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_assume[0].json

  tags = var.tags
}

data "aws_iam_policy_document" "execution" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPull"
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [var.ecr_repository_arn]
  }

  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.exec[0].arn,
      "${aws_cloudwatch_log_group.exec[0].arn}:log-stream:*",
    ]
  }

  dynamic "statement" {
    for_each = length(var.secrets) == 0 ? [] : [1]
    content {
      sid       = "Secrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [for s in var.secrets : s.secret_arn]
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  count = local.enabled ? 1 : 0

  name   = "execution"
  role   = aws_iam_role.execution[0].id
  policy = data.aws_iam_policy_document.execution[0].json
}

data "aws_iam_policy_document" "task" {
  count = local.enabled ? 1 : 0

  statement {
    sid = "EcsExecChannel"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "DecryptExecChannel"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.ecs_exec[0].arn]
  }

  statement {
    sid = "ExecLoggingCloudWatch"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      aws_cloudwatch_log_group.exec[0].arn,
      "${aws_cloudwatch_log_group.exec[0].arn}:log-stream:*",
    ]
  }

  statement {
    sid       = "ExecLoggingDescribeGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid       = "ExecLoggingS3Put"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.exec_logs[0].arn}/*"]
  }

  statement {
    sid       = "ExecLoggingS3Bucket"
    actions   = ["s3:GetBucketLocation", "s3:GetEncryptionConfiguration"]
    resources = [aws_s3_bucket.exec_logs[0].arn]
  }
}

resource "aws_iam_role_policy" "task" {
  count = local.enabled ? 1 : 0

  name   = "task"
  role   = aws_iam_role.task[0].id
  policy = data.aws_iam_policy_document.task[0].json
}
