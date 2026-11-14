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

  exceptions_enabled     = local.enabled && var.config_store != null
  exception_subject      = "${local.subject_prefix}:environment:${var.exception_environment}"
  config_bucket_arn      = try(var.config_store.bucket_arn, "")
  config_kms_key_arn     = try(var.config_store.kms_key_arn, "")
  config_region          = try(split(":", var.config_store.kms_key_arn)[3], "")
  platform_exceptions    = "${local.config_bucket_arn}/platform/image_exceptions.yaml"
  platform_policy        = "${local.config_bucket_arn}/platform/image_policy.yaml"
  config_key_through_s3  = { StringEquals = { "kms:ViaService" = "s3.${local.config_region}.amazonaws.com" } }
  exception_pull_actions = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]

  # The daily review prunes the platform exceptions and reports every tenant's.
  review_statements = [for statement in [
    {
      Sid      = "PruneThePlatformExceptions"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
      Resource = local.platform_exceptions
    },
    {
      Sid      = "ReadTheImagePolicyAndTheTenantExceptions"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:GetObjectVersion"]
      Resource = [local.platform_policy, "${local.config_bucket_arn}/tenants/*/image_exceptions.yaml"]
    },
    {
      Sid       = "ListTheTenantPrefixes"
      Effect    = "Allow"
      Action    = "s3:ListBucket"
      Resource  = local.config_bucket_arn
      Condition = { StringLike = { "s3:prefix" = ["tenants/*"] } }
    },
    {
      Sid       = "UseTheConfigKeyThroughS3"
      Effect    = "Allow"
      Action    = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource  = local.config_kms_key_arn
      Condition = local.config_key_through_s3
    },
  ] : statement if local.exceptions_enabled]
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
      local.review_statements,
    )
  })
}

resource "aws_iam_role" "exception" {
  count = local.exceptions_enabled ? 1 : 0

  name        = "${var.name_prefix}-github-image-exception"
  description = "Assumed by the image-exception workflow of ${var.github_repository} in the ${var.exception_environment} environment to accept a finding in a platform image."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubWorkflowsOfOneRepositoryAndEnvironment"
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.provider_arn }
      Condition = {
        StringEquals = {
          "${local.issuer}:aud" = "sts.amazonaws.com"
          "${local.issuer}:sub" = local.exception_subject
        }
      }
    }]
  })
  max_session_duration = 3600
  tags                 = var.tags
}

resource "aws_iam_role_policy" "exception" {
  count = local.exceptions_enabled ? 1 : 0

  name = "write-platform-exceptions"
  role = aws_iam_role.exception[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthorization"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "ScanTheImage"
        Effect   = "Allow"
        Action   = "inspector-scan:ScanSbom"
        Resource = "*"
      },
      {
        Sid      = "PullThePlatformImages"
        Effect   = "Allow"
        Action   = local.exception_pull_actions
        Resource = concat(var.repository_arns, var.pull_repository_arns)
      },
      {
        Sid      = "WriteThePlatformExceptions"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = local.platform_exceptions
      },
      {
        Sid      = "ReadTheImagePolicy"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = local.platform_policy
      },
      {
        Sid       = "UseTheConfigKeyThroughS3"
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource  = local.config_kms_key_arn
        Condition = local.config_key_through_s3
      },
    ]
  })
}
