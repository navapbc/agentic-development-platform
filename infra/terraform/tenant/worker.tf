# Attached to the tenant's Pod Identity role. It reads only this tenant's secrets, bundle, and images.
resource "aws_iam_policy" "worker" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.name}-worker-"
  description = "Lets the ${var.tenant} tenant's activity worker read its secrets, config bundle, and images."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      length(var.secrets) == 0 ? [] : [{
        Sid      = "ReadTheTenantSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [for secret in aws_secretsmanager_secret.this : secret.arn]
      }],
      [
        {
          Sid       = "DecryptTheTenantSecrets"
          Effect    = "Allow"
          Action    = "kms:Decrypt"
          Resource  = local.kms_key_arn
          Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${local.region}.amazonaws.com" } }
        },
        {
          Sid       = "ListTheTenantBundle"
          Effect    = "Allow"
          Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
          Resource  = var.config_store.bucket_arn
          Condition = { StringLike = { "s3:prefix" = ["${local.config_prefix}*"] } }
        },
        {
          Sid      = "ReadTheTenantBundle"
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:GetObjectVersion"]
          Resource = "${var.config_store.bucket_arn}/${local.config_prefix}*"
        },
        {
          Sid       = "DecryptTheTenantBundle"
          Effect    = "Allow"
          Action    = "kms:Decrypt"
          Resource  = var.config_store.kms_key_arn
          Condition = { StringEquals = { "kms:ViaService" = "s3.${local.region}.amazonaws.com" } }
        },
        {
          Sid      = "PullTheTenantImages"
          Effect   = "Allow"
          Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
          Resource = local.image_repos_arn
        },
      ],
    )
  })

  tags = local.tags
}
