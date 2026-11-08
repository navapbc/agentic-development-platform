locals {
  account_root = "arn:${local.partition}:iam::${local.account_id}:root"

  key_administration = {
    Sid       = "EnableAccountKeyAdministration"
    Effect    = "Allow"
    Principal = { AWS = local.account_root }
    Action    = "kms:*"
    Resource  = "*"
  }

  # The Auto Scaling service-linked role exists only after the account's first Auto Scaling
  # group, and a key policy that names a missing principal is rejected, so the statements match
  # the role by aws:PrincipalArn.
  autoscaling_role_arn = "arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
}

resource "aws_kms_key" "secrets" {
  count = local.enabled ? 1 : 0

  description         = "Envelope-encrypts the Kubernetes secrets of ${local.cluster_name}."
  enable_key_rotation = true
  policy              = jsonencode({ Version = "2012-10-17", Statement = [local.key_administration] })

  tags = merge(var.tags, { Name = "${local.cluster_name}-secrets" })
}

resource "aws_kms_alias" "secrets" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.cluster_name}-secrets"
  target_key_id = aws_kms_key.secrets[0].id
}

resource "aws_kms_key" "logs" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the control plane log group of ${local.cluster_name}."
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.key_administration,
      {
        Sid       = "AllowCloudWatchLogsForTheControlPlaneLogGroup"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.log_group_name}"
          }
        }
      },
    ]
  })

  tags = merge(var.tags, { Name = "${local.cluster_name}-logs" })
}

resource "aws_kms_alias" "logs" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.cluster_name}-logs"
  target_key_id = aws_kms_key.logs[0].id
}

resource "aws_kms_key" "ebs" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the EBS volumes of the ${local.cluster_name} nodes."
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.key_administration,
      {
        Sid       = "AllowAutoScalingToUseTheKeyForNodeVolumes"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
        Condition = {
          ArnEquals    = { "aws:PrincipalArn" = local.autoscaling_role_arn }
          StringEquals = { "kms:CallerAccount" = local.account_id }
        }
      },
      {
        Sid       = "AllowAutoScalingToGrantTheKeyToEc2"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = {
          ArnEquals    = { "aws:PrincipalArn" = local.autoscaling_role_arn }
          StringEquals = { "kms:CallerAccount" = local.account_id }
          Bool         = { "kms:GrantIsForAWSResource" = "true" }
        }
      },
    ]
  })

  tags = merge(var.tags, { Name = "${local.cluster_name}-ebs" })
}

resource "aws_kms_alias" "ebs" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.cluster_name}-ebs"
  target_key_id = aws_kms_key.ebs[0].id
}
