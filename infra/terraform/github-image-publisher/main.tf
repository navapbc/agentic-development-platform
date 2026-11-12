locals {
  enabled         = var.create_github_image_publisher
  create_provider = local.enabled && var.oidc_provider_arn == ""
  issuer          = "token.actions.githubusercontent.com"
  provider_arn    = local.create_provider ? aws_iam_openid_connect_provider.github[0].arn : var.oidc_provider_arn
  owner           = split("/", var.github_repository)[0]
  repository_name = split("/", var.github_repository)[1]
  subject_prefix = (
    var.github_repository_ids == null
    ? "repo:${var.github_repository}"
    : "repo:${local.owner}@${var.github_repository_ids.owner_id}/${local.repository_name}@${var.github_repository_ids.repository_id}"
  )
  subjects = [for ref in var.allowed_refs : "${local.subject_prefix}:ref:${ref}"]
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.create_provider ? 1 : 0

  url            = "https://${local.issuer}"
  client_id_list = ["sts.amazonaws.com"]
  tags           = merge(var.tags, { Name = "${var.name_prefix}-github-oidc" })
}

resource "aws_iam_role" "publisher" {
  count = local.enabled ? 1 : 0

  name        = "${var.name_prefix}-github-image-publisher"
  description = "Assumed by the publish-images workflow of ${var.github_repository} to push the platform images."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubWorkflowsOfOneRepositoryAndRef"
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.provider_arn }
      Condition = {
        StringEquals = { "${local.issuer}:aud" = "sts.amazonaws.com" }
        StringLike   = { "${local.issuer}:sub" = local.subjects }
      }
    }]
  })
  max_session_duration = 3600
  tags                 = var.tags

  lifecycle {
    precondition {
      condition     = length(var.repository_arns) > 0
      error_message = "The image publisher needs the platform ECR repository ARNs. Enable create_container_services."
    }
  }
}

resource "aws_iam_role_policy" "push" {
  count = local.enabled ? 1 : 0

  name = "push-platform-images"
  role = aws_iam_role.publisher[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "EcrAuthorization"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "ScanImagesBeforeThePush"
        Effect   = "Allow"
        Action   = "inspector-scan:ScanSbom"
        Resource = "*"
      },
      {
        Sid    = "PushThePlatformImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = var.repository_arns
      },
      ],
      var.signing_profile_arn == "" ? [] : [{
        Sid      = "SignThePlatformImages"
        Effect   = "Allow"
        Action   = ["signer:SignPayload", "signer:GetSigningProfile"]
        Resource = var.signing_profile_arn
      }],
    )
  })
}
