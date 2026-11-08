data "aws_caller_identity" "current" {
  count = local.enabled ? 1 : 0
}

data "aws_partition" "current" {
  count = local.enabled ? 1 : 0
}

data "aws_region" "current" {
  count = local.enabled ? 1 : 0
}

locals {
  enabled    = var.create_tenant
  account_id = try(data.aws_caller_identity.current[0].account_id, "")
  partition  = try(data.aws_partition.current[0].partition, "aws")
  region     = try(data.aws_region.current[0].region, "")
  tags       = merge(var.tags, { tenant = var.tenant })
  name       = "${var.name_prefix}-tenant-${var.tenant}"

  secret_prefix   = "${var.name_prefix}/tenants/${var.tenant}/"
  image_prefix    = "${var.name_prefix}/tenants/${var.tenant}"
  image_repos_arn = "arn:${local.partition}:ecr:${local.region}:${local.account_id}:repository/${local.image_prefix}/*"
  config_prefix   = "tenants/${var.tenant}/"
  kms_key_arn     = try(aws_kms_key.this[0].arn, "")
}

resource "terraform_data" "inputs_guard" {
  count = local.enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.worker_role_arn != ""
      error_message = "A tenant needs worker_role_arn, the Pod Identity role of its activity worker."
    }
    precondition {
      condition     = var.config_store != null
      error_message = "A tenant needs config_store, the bucket that holds its config bundle."
    }
  }
}

resource "aws_kms_key" "this" {
  count = local.enabled ? 1 : 0

  description             = "Encrypts the secrets and images of the ${var.tenant} tenant."
  enable_key_rotation     = true
  deletion_window_in_days = 30
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
        Sid       = "DecryptTheTenantSecretsForItsWorker"
        Effect    = "Allow"
        Principal = { AWS = var.worker_role_arn }
        Action    = "kms:Decrypt"
        Resource  = "*"
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${local.region}.amazonaws.com" } }
      },
    ]
  })

  tags = merge(local.tags, { Name = local.name })
}

resource "aws_kms_alias" "this" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.this[0].id
}

resource "aws_secretsmanager_secret" "this" {
  for_each = local.enabled ? toset(var.secrets) : toset([])

  name                    = "${local.secret_prefix}${replace(each.key, "_", "-")}"
  description             = "${each.key} of the ${var.tenant} tenant, which only its activity worker reads."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

resource "aws_secretsmanager_secret_policy" "this" {
  for_each = aws_secretsmanager_secret.this

  secret_arn          = each.value.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheTenantWorker"
        Effect    = "Allow"
        Principal = { AWS = var.worker_role_arn }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.worker_role_arn } }
      },
    ]
  })
}
