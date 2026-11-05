data "archive_file" "rotation" {
  count = local.rotation_enabled ? 1 : 0

  type             = "zip"
  source_file      = "${path.module}/functions/rotation.py"
  output_path      = "${path.root}/.terraform/build/${var.name_prefix}-secret-rotation.zip"
  output_file_mode = "0644"
}

data "aws_iam_policy_document" "lambda_assume" {
  count = local.rotation_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_security_group" "rotation" {
  count = local.rotation_enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-secret-rotation-"
  description = "Secret rotation functions. No ingress. Egress 443 to the shared VPC endpoints only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-secret-rotation" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "rotation_to_endpoints" {
  count = local.rotation_enabled ? 1 : 0

  security_group_id            = aws_security_group.rotation[0].id
  description                  = "HTTPS to the shared VPC endpoints (Secrets Manager, RDS Data API, ElastiCache)"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

locals {
  rotation_functions = merge(
    local.database_enabled ? {
      postgres = {
        handler = "rotation.postgres_handler"
        role    = try(aws_iam_role.postgres[0].arn, "")
        environment = {
          LOG_LEVEL         = "INFO"
          CLUSTER_ARN       = var.database_cluster_arn
          MASTER_SECRET_ARN = var.database_master_secret_arn
          PLATFORM_DATABASE = var.platform_database
          PLATFORM_SCHEMA   = var.platform_schema
          ROLE_SECRET_ARNS  = jsonencode({ for role, key in local.database_secret_keys : role => aws_secretsmanager_secret.this[key].arn })
        }
      }
    } : {},
    local.cache_enabled ? {
      elasticache = {
        handler     = "rotation.elasticache_handler"
        role        = try(aws_iam_role.elasticache[0].arn, "")
        environment = { LOG_LEVEL = "INFO" }
      }
    } : {},
  )
}

resource "aws_cloudwatch_log_group" "rotation" {
  for_each = local.rotation_functions

  name              = "/aws/lambda/${var.name_prefix}-${each.key}-rotation"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this[0].arn
  tags              = var.tags
}

resource "aws_lambda_function" "rotation" {
  for_each = local.rotation_functions

  function_name    = "${var.name_prefix}-${each.key}-rotation"
  description      = "Rotates the ${each.key} credentials in Secrets Manager."
  role             = each.value.role
  runtime          = "python3.13"
  handler          = each.value.handler
  filename         = data.archive_file.rotation[0].output_path
  source_code_hash = data.archive_file.rotation[0].output_base64sha256
  timeout          = 900
  memory_size      = 256
  kms_key_arn      = aws_kms_key.this[0].arn

  environment {
    variables = each.value.environment
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.rotation[0].id]
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.rotation[each.key].name
  }

  tracing_config {
    mode = "Active"
  }

  tags = var.tags

  depends_on = [
    aws_iam_role_policy.postgres,
    aws_iam_role_policy.elasticache,
    aws_iam_role_policy_attachment.rotation_vpc_access,
  ]
}

resource "aws_iam_role" "postgres" {
  count = local.database_enabled ? 1 : 0

  name                 = "${var.name_prefix}-postgres-rotation"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume[0].json

  tags = var.tags
}

resource "aws_iam_role" "elasticache" {
  count = local.cache_enabled ? 1 : 0

  name                 = "${var.name_prefix}-elasticache-rotation"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume[0].json

  tags = var.tags
}

locals {
  rotation_role_names = merge(
    local.database_enabled ? { postgres = aws_iam_role.postgres[0].name } : {},
    local.cache_enabled ? { elasticache = aws_iam_role.elasticache[0].name } : {},
  )
}

resource "aws_iam_role_policy_attachment" "rotation_vpc_access" {
  for_each = local.rotation_role_names

  role       = each.value
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "postgres" {
  count = local.database_enabled ? 1 : 0

  statement {
    sid = "RotateServiceCredentials"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
      "secretsmanager:UpdateSecretVersionStage",
    ]
    resources = [for key in values(local.database_secret_keys) : aws_secretsmanager_secret.this[key].arn]
  }

  statement {
    sid       = "ReadMasterCredential"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.database_master_secret_arn]
  }

  statement {
    sid       = "GeneratePasswords"
    actions   = ["secretsmanager:GetRandomPassword"]
    resources = ["*"]
  }

  statement {
    sid = "RunStatementsThroughDataApi"
    actions = [
      "rds-data:ExecuteStatement",
      "rds-data:BeginTransaction",
      "rds-data:CommitTransaction",
      "rds-data:RollbackTransaction",
    ]
    resources = [var.database_cluster_arn]
  }

  statement {
    sid       = "UseSecretKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.this[0].arn]
  }

  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "postgres" {
  count = local.database_enabled ? 1 : 0

  name   = "rotation"
  role   = aws_iam_role.postgres[0].id
  policy = data.aws_iam_policy_document.postgres[0].json
}

data "aws_iam_policy_document" "elasticache" {
  count = local.cache_enabled ? 1 : 0

  statement {
    sid = "RotateUserCredential"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
      "secretsmanager:UpdateSecretVersionStage",
    ]
    resources = [var.elasticache_user_secret_arn]
  }

  statement {
    sid       = "GeneratePasswords"
    actions   = ["secretsmanager:GetRandomPassword"]
    resources = ["*"]
  }

  statement {
    sid       = "ChangeUserPassword"
    actions   = ["elasticache:ModifyUser"]
    resources = [var.elasticache_user_arn]
  }

  statement {
    sid       = "ReadUserStatus"
    actions   = ["elasticache:DescribeUsers"]
    resources = ["*"]
  }

  statement {
    sid       = "UseSecretKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.this[0].arn]
  }

  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "elasticache" {
  count = local.cache_enabled ? 1 : 0

  name   = "rotation"
  role   = aws_iam_role.elasticache[0].id
  policy = data.aws_iam_policy_document.elasticache[0].json
}

locals {
  rotated_secrets = merge(
    { for role, key in local.database_secret_keys : key => "postgres" },
    local.cache_enabled ? { elasticache_user = "elasticache" } : {},
  )
}

resource "aws_lambda_permission" "rotation" {
  for_each = local.rotated_secrets

  statement_id   = "SecretsManagerRotation-${replace(each.key, "_", "-")}"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.rotation[each.value].function_name
  principal      = "secretsmanager.amazonaws.com"
  source_arn     = local.secret_arns[each.key]
  source_account = local.account_id
}

resource "aws_secretsmanager_secret_rotation" "this" {
  for_each = local.rotated_secrets

  secret_id           = local.secret_arns[each.key]
  rotation_lambda_arn = aws_lambda_function.rotation[each.value].arn
  rotate_immediately  = false

  rotation_rules {
    schedule_expression = var.rotation_schedule_expression
    duration            = var.rotation_window
  }

  depends_on = [
    aws_lambda_permission.rotation,
    aws_secretsmanager_secret_version.database,
    aws_secretsmanager_secret_policy.this,
  ]
}
