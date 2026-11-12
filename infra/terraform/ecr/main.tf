data "aws_caller_identity" "current" {}

locals {
  enabled        = var.create_repository
  create_kms_key = local.enabled && var.kms_key_arn == ""
  kms_key_arn    = local.create_kms_key ? aws_kms_key.this[0].arn : var.kms_key_arn
}

data "aws_iam_policy_document" "kms" {
  count = local.create_kms_key ? 1 : 0

  statement {
    sid       = "EnableAccountKeyAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "this" {
  count = local.create_kms_key ? 1 : 0

  description         = "Encrypts the ${var.repository_name} ECR repository."
  policy              = data.aws_iam_policy_document.kms[0].json
  enable_key_rotation = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-${replace(var.repository_name, "/", "-")}-ecr-kms" })
}

resource "aws_ecr_repository" "this" {
  count = local.enabled ? 1 : 0

  name                 = var.repository_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = local.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-${replace(var.repository_name, "/", "-")}" })
}

resource "aws_ecr_lifecycle_policy" "this" {
  count = local.enabled ? 1 : 0

  repository = aws_ecr_repository.this[0].name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images."
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_expiry_days
        }
        action = { type = "expire" }
      }
    ]
  })
}
