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

  claims_probe_enabled    = local.publisher_enabled && try(var.config_publisher.pull_request_subject, null) != null
  claim_digest_list_scope = { StringLike = { "s3:prefix" = ["claims/*/*/"] } }
  claim_index_arn         = "${try(var.config_store.bucket_arn, "")}/claims/_owners/${var.tenant}.json"
  claims_protocol_arn     = "${try(var.config_store.bucket_arn, "")}/platform/claims-protocol.json"
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

  tags = merge(local.tags, { (var.config_store.publisher_tag_key) = var.tenant })
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
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = var.config_store.bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["${local.config_prefix}*"] } }
      },
      {
        Sid      = "PublishTheTenantBundle"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectTagging", "s3:DeleteObject"]
        Resource = "${var.config_store.bucket_arn}/${local.config_prefix}*"
      },
      {
        Sid       = "ListOneClaimDigest"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = var.config_store.bucket_arn
        Condition = local.claim_digest_list_scope
      },
      {
        Sid      = "ManageTheTenantClaims"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = ["${var.config_store.bucket_arn}/claims/*/*/${var.tenant}", local.claim_index_arn]
      },
      {
        Sid      = "ReadTheTenantClaimIndex"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = local.claim_index_arn
      },
      {
        Sid      = "ReadTheClaimsProtocol"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = local.claims_protocol_arn
      },
      {
        Sid       = "EncryptTheTenantBundle"
        Effect    = "Allow"
        Action    = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource  = var.config_store.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${local.region}.amazonaws.com" } }
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
      {
        Sid      = "SignTheTenantImages"
        Effect   = "Allow"
        Action   = ["signer:SignPayload", "signer:GetSigningProfile"]
        Resource = aws_signer_signing_profile.this[0].arn
      },
    ]
  })
}

# ECR authorizes GetAuthorizationToken only on the * resource, so it stays out of the policy that
# names the tenant's objects.
resource "aws_iam_role_policy" "publisher_ecr_sign_in" {
  count = local.publisher_enabled ? 1 : 0

  name = "sign-in-to-ecr"
  role = aws_iam_role.publisher[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "EcrAuthorization"
      Effect   = "Allow"
      Action   = "ecr:GetAuthorizationToken"
      Resource = "*"
    }]
  })
}

resource "aws_iam_role" "claims_probe" {
  count = local.claims_probe_enabled ? 1 : 0

  name        = "${local.name}-claims-probe"
  description = "Assumed by the pull request workflows of ${var.config_publisher.github_repository} to check the ${var.tenant} tenant's claims against the other tenants."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "GitHubPullRequestsOfOneRepository"
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.config_publisher.oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.publisher_issuer}:aud" = "sts.amazonaws.com"
          "${local.publisher_issuer}:sub" = "${local.publisher_subject_prefix}:${var.config_publisher.pull_request_subject}"
        }
      }
    }]
  })
  max_session_duration = 3600

  tags = local.tags
}

resource "aws_iam_role_policy" "claims_probe" {
  count = local.claims_probe_enabled ? 1 : 0

  name = "list-one-claim-digest"
  role = aws_iam_role.claims_probe[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListOneClaimDigest"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = var.config_store.bucket_arn
        Condition = local.claim_digest_list_scope
      },
      {
        Sid      = "ReadTheClaimsProtocol"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = local.claims_protocol_arn
      },
      {
        Sid       = "DecryptThroughS3"
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = var.config_store.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${local.region}.amazonaws.com" } }
      },
    ]
  })
}
