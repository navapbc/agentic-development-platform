locals {
  publisher_enabled = local.enabled && var.config_publisher != null
  publisher_issuer  = try(regex("^arn:[^:]+:iam::[0-9]+:oidc-provider/(.+)$", var.config_publisher.oidc_provider_arn)[0], "")
  publisher_owner   = try(split("/", var.config_publisher.github_repository)[0], "")
  publisher_repo    = try(split("/", var.config_publisher.github_repository)[1], "")
  publisher_subject_prefix = try(
    "repo:${local.publisher_owner}@${var.config_publisher.github_repository_ids.owner_id}/${local.publisher_repo}@${var.config_publisher.github_repository_ids.repository_id}",
    "repo:${try(var.config_publisher.github_repository, "")}",
  )
  publisher_subjects = [for ref in try(var.config_publisher.allowed_refs, []) : "${local.publisher_subject_prefix}:ref:${ref}"]
}

resource "aws_iam_role" "publisher" {
  count = local.publisher_enabled ? 1 : 0

  name        = "${local.name}-publisher"
  description = "Assumed by the workflows of ${var.config_publisher.github_repository} to publish the ${var.tenant} tenant's config bundle and images."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubWorkflowsOfOneRepositoryAndRef"
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.config_publisher.oidc_provider_arn }
      Condition = {
        StringEquals = { "${local.publisher_issuer}:aud" = "sts.amazonaws.com" }
        StringLike   = { "${local.publisher_issuer}:sub" = local.publisher_subjects }
      }
    }]
  })
  max_session_duration = 3600

  tags = local.tags
}

resource "aws_iam_role_policy" "publisher" {
  count = local.publisher_enabled ? 1 : 0

  name = "publish-tenant-config-and-images"
  role = aws_iam_role.publisher[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListTheTenantBundle"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = var.config_store.bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["${local.config_prefix}*"] } }
      },
      {
        Sid      = "PublishTheTenantBundle"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = "${var.config_store.bucket_arn}/${local.config_prefix}*"
      },
      {
        Sid       = "EncryptTheTenantBundle"
        Effect    = "Allow"
        Action    = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource  = var.config_store.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${local.region}.amazonaws.com" } }
      },
      {
        Sid      = "EcrAuthorization"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushTheTenantImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:CreateRepository",
          "ecr:DescribeImages",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = local.image_repos_arn
      },
    ]
  })
}
