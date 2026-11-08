# A tenant's CI pushes to <name_prefix>/tenants/<tenant>/<image>, and ECR creates the repository on
# the first push from this template. ECR assumes the creation role to apply the tenant key and the
# tags, without which the creation fails.
resource "aws_iam_role" "image_creation" {
  count = local.enabled ? 1 : 0

  name        = "${local.name}-image-creation"
  description = "Assumed by ECR to create the ${var.tenant} tenant's repositories with its key and tags."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecr.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "image_creation" {
  count = local.enabled ? 1 : 0

  name = "create-tenant-repositories"
  role = aws_iam_role.image_creation[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CreateAndTagTheTenantRepositories"
        Effect   = "Allow"
        Action   = ["ecr:CreateRepository", "ecr:TagResource"]
        Resource = local.image_repos_arn
      },
      {
        Sid      = "EncryptWithTheTenantKey"
        Effect   = "Allow"
        Action   = ["kms:CreateGrant", "kms:RetireGrant", "kms:DescribeKey"]
        Resource = local.kms_key_arn
      },
    ]
  })
}

resource "aws_ecr_repository_creation_template" "this" {
  count = local.enabled ? 1 : 0

  prefix               = local.image_prefix
  description          = "Repositories the ${var.tenant} tenant's CI creates on its first push."
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "IMMUTABLE"
  custom_role_arn      = aws_iam_role.image_creation[0].arn
  resource_tags        = local.tags

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = local.kms_key_arn
  }

  lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images."
      selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = var.untagged_image_expiry_days }
      action       = { type = "expire" }
    }]
  })

  depends_on = [aws_iam_role_policy.image_creation]
}
