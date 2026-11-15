locals {
  repo_intelligence_token_readers = concat([var.worker_role_arn], var.repo_intelligence_reader_arns)
  repo_intelligence_token_arn     = try(aws_secretsmanager_secret.repo_intelligence_token[0].arn, "")
  repo_intelligence_token_rotator = try(var.repo_intelligence_token_rotation.role_arn, null)
  repo_intelligence_token_getters = concat(local.repo_intelligence_token_readers, local.repo_intelligence_token_rotator == null ? [] : [local.repo_intelligence_token_rotator])
}

resource "aws_secretsmanager_secret" "repo_intelligence_token" {
  count = local.enabled ? 1 : 0

  name                    = "${local.secret_prefix}repo-intelligence-token"
  description             = "Bearer token that binds the ${var.tenant} tenant's repository intelligence requests to the tenant. Its activity worker sends it, and repository intelligence maps it to the tenant."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

ephemeral "random_password" "repo_intelligence_token" {
  count = local.enabled ? 1 : 0

  length  = 48
  special = false
}

resource "aws_secretsmanager_secret_version" "repo_intelligence_token" {
  count = local.enabled ? 1 : 0

  secret_id                = aws_secretsmanager_secret.repo_intelligence_token[0].id
  secret_string_wo         = ephemeral.random_password.repo_intelligence_token[0].result
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret_policy" "repo_intelligence_token" {
  count = local.enabled ? 1 : 0

  secret_arn          = aws_secretsmanager_secret.repo_intelligence_token[0].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid       = "AllowTheTenantWorkerAndRepositoryIntelligence"
          Effect    = "Allow"
          Principal = { AWS = local.repo_intelligence_token_readers }
          Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
          Resource  = "*"
        },
        {
          Sid       = "DenyOtherReaders"
          Effect    = "Deny"
          Principal = { AWS = "*" }
          Action    = "secretsmanager:GetSecretValue"
          Resource  = "*"
          Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.repo_intelligence_token_getters } }
        },
      ],
      local.repo_intelligence_token_rotator == null ? [] : [
        {
          Sid       = "AllowTheRotationFunction"
          Effect    = "Allow"
          Principal = { AWS = local.repo_intelligence_token_rotator }
          Action = [
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
            "secretsmanager:PutSecretValue",
            "secretsmanager:UpdateSecretVersionStage",
          ]
          Resource = "*"
        },
      ],
    )
  })
}

# Rotation stages each new token as AWSCURRENT. The version above keeps the initial token, which
# Secrets Manager labels AWSPREVIOUS and then deprecates, and its write-only version never changes,
# so a rotation causes no plan difference.
resource "aws_secretsmanager_secret_rotation" "repo_intelligence_token" {
  count = local.enabled && var.repo_intelligence_token_rotation != null ? 1 : 0

  secret_id           = aws_secretsmanager_secret.repo_intelligence_token[0].id
  rotation_lambda_arn = var.repo_intelligence_token_rotation.function_arn
  rotate_immediately  = false

  rotation_rules {
    automatically_after_days = var.repo_intelligence_token_rotation_days
  }

  depends_on = [
    aws_secretsmanager_secret_version.repo_intelligence_token,
    aws_secretsmanager_secret_policy.repo_intelligence_token,
  ]
}
