locals {
  account_root = "arn:${local.partition}:iam::${local.account_id}:root"

  logs_key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = local.account_root }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogsForTheClusterLogGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = [
              "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.exec_log_group_name}",
              "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.insights_log_group_name}",
            ]
          }
        }
      },
    ]
  })

  storage_encryption_context = {
    "kms:EncryptionContext:aws:ecs:clusterAccount" = local.account_id
    "kms:EncryptionContext:aws:ecs:clusterName"    = local.cluster_name
  }

  storage_key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = local.account_root }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowFargateToGenerateEphemeralStorageKeys"
        Effect    = "Allow"
        Principal = { Service = "fargate.amazonaws.com" }
        Action    = "kms:GenerateDataKeyWithoutPlaintext"
        Resource  = "*"
        Condition = { StringEquals = local.storage_encryption_context }
      },
      {
        Sid       = "AllowFargateToGrantEphemeralStorageDecrypt"
        Effect    = "Allow"
        Principal = { Service = "fargate.amazonaws.com" }
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = {
          StringEquals                = local.storage_encryption_context
          "ForAllValues:StringEquals" = { "kms:GrantOperations" = ["Decrypt"] }
        }
      },
      {
        Sid       = "AllowFargateToDescribeTheKey"
        Effect    = "Allow"
        Principal = { Service = "fargate.amazonaws.com" }
        Action    = "kms:DescribeKey"
        Resource  = "*"
      },
    ]
  })
}

resource "aws_kms_key" "logs" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the ECS Exec sessions and the Exec and Container Insights log groups of ${local.cluster_name}."
  policy              = local.logs_key_policy
  enable_key_rotation = true

  tags = merge(var.tags, { Name = "${local.cluster_name}-logs" })
}

resource "aws_kms_alias" "logs" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.cluster_name}-logs"
  target_key_id = aws_kms_key.logs[0].id
}

resource "aws_kms_key" "storage" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the Fargate ephemeral storage and managed storage of the tasks in ${local.cluster_name}."
  policy              = local.storage_key_policy
  enable_key_rotation = true

  tags = merge(var.tags, { Name = "${local.cluster_name}-storage" })
}

resource "aws_kms_alias" "storage" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.cluster_name}-storage"
  target_key_id = aws_kms_key.storage[0].id
}
