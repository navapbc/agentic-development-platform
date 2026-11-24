locals {
  # The review worker starts the tenant's gate and scanner pods, which authenticate to an external
  # registry with the tenant's registry token.
  review_tenant_secrets = [for name in var.secrets : name if contains(["registry_token"], name)]
  review_secret_arns = concat(
    aws_secretsmanager_secret.review_signing_key[*].arn,
    [for name in local.review_tenant_secrets : aws_secretsmanager_secret.this[name].arn],
  )
  review_signing_key_arn = try(aws_secretsmanager_secret.review_signing_key[0].arn, "")
}

resource "terraform_data" "review_inputs_guard" {
  count = local.enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.review_role_arn != "" && var.review_role_arn != var.worker_role_arn
      error_message = "A tenant needs review_role_arn, the Pod Identity role of its review worker, which is not its activity worker's role."
    }
  }
}

resource "aws_secretsmanager_secret" "review_signing_key" {
  count = local.enabled ? 1 : 0

  name                    = "${local.secret_prefix}review-signing-key"
  description             = "OpenSSH private key that signs the review verdicts of the ${var.tenant} tenant, which only its review worker reads. Its public key is an active key of the tenant in review-trust-root.yaml."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

resource "aws_secretsmanager_secret_policy" "review_signing_key" {
  count = local.enabled ? 1 : 0

  secret_arn          = aws_secretsmanager_secret.review_signing_key[0].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheTenantReviewWorker"
        Effect    = "Allow"
        Principal = { AWS = var.review_role_arn }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.review_role_arn } }
      },
    ]
  })
}

# Attached to the tenant's review worker role. It reads only this tenant's review secrets, bundle,
# and images, and decrypts the tenant key for those secrets alone.
resource "aws_iam_policy" "review" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.name}-review-"
  description = "Lets the ${var.tenant} tenant's review worker read its signing key and registry token, its config bundle, and its images, and write the evidence bucket."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "ReadTheReviewSecrets"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
          Resource = local.review_secret_arns
        },
        {
          Sid      = "DecryptTheReviewSecrets"
          Effect   = "Allow"
          Action   = "kms:Decrypt"
          Resource = local.kms_key_arn
          Condition = {
            StringEquals = { "kms:ViaService" = "secretsmanager.${local.region}.amazonaws.com" }
            ArnEquals    = { "kms:EncryptionContext:SecretARN" = local.review_secret_arns }
          }
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
      var.evidence_store == null ? [] : [
        {
          Sid      = "WriteTheEvidence"
          Effect   = "Allow"
          Action   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:GetObjectRetention"]
          Resource = "${var.evidence_store.bucket_arn}/*"
        },
      ],
      try(var.evidence_store.kms_key_arn, "") == "" ? [] : [
        {
          Sid      = "EncryptTheEvidence"
          Effect   = "Allow"
          Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
          Resource = var.evidence_store.kms_key_arn
        },
      ],
    )
  })

  tags = local.tags
}
