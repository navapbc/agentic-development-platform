data "aws_caller_identity" "current" {
  count = local.enabled ? 1 : 0
}

data "aws_partition" "current" {
  count = local.enabled ? 1 : 0
}

data "aws_region" "current" {
  count = local.enabled ? 1 : 0
}

locals {
  enabled    = var.create_tenant_config_store
  account_id = try(data.aws_caller_identity.current[0].account_id, "")
  partition  = try(data.aws_partition.current[0].partition, "aws")
  region     = try(data.aws_region.current[0].region, "")

  bucket_arn  = try(aws_s3_bucket.this[0].arn, "")
  kms_key_arn = try(aws_kms_key.this[0].arn, "")

  platform_exceptions_key = "platform/image_exceptions.yaml"
  platform_policy_key     = "platform/image_policy.yaml"
}

resource "aws_kms_key" "this" {
  count = local.enabled ? 1 : 0

  description             = "Encrypts the ${var.name_prefix} tenant config bucket."
  policy                  = local.key_policy
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(var.tags, { Name = "${var.name_prefix}-tenant-config" })
}

resource "aws_kms_alias" "this" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-tenant-config"
  target_key_id = aws_kms_key.this[0].id
}

resource "aws_s3_bucket" "this" {
  count = local.enabled ? 1 : 0

  bucket_prefix = "${var.name_prefix}-tenant-config-"

  tags = merge(var.tags, { Name = "${var.name_prefix}-tenant-config" })
}

resource "aws_s3_bucket_ownership_controls" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id

  rule {
    id     = "expire-replaced-config-and-abandoned-uploads"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this[0].arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  count = local.enabled ? 1 : 0

  bucket                  = aws_s3_bucket.this[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "this" {
  count = local.enabled ? 1 : 0

  bucket        = aws_s3_bucket.this[0].id
  target_bucket = var.access_log_bucket
  target_prefix = "${var.name_prefix}-tenant-config/"
}

resource "aws_s3_bucket_policy" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  policy = local.bucket_policy

  depends_on = [aws_s3_bucket_public_access_block.this]
}

# The platform exceptions file starts empty. The daily review and the image-exception workflow
# replace it through conditional writes, so a later apply must never write it again.
resource "aws_s3_object" "platform_exceptions" {
  count = local.enabled ? 1 : 0

  bucket                 = aws_s3_bucket.this[0].id
  key                    = local.platform_exceptions_key
  content                = "config_version: 1\nexceptions: []\n"
  content_type           = "application/yaml"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.this[0].arn

  lifecycle {
    ignore_changes = [content, etag]
  }

  depends_on = [
    aws_s3_bucket_versioning.this,
    aws_s3_bucket_server_side_encryption_configuration.this,
    aws_s3_bucket_policy.this,
  ]
}

resource "aws_s3_object" "platform_image_policy" {
  count = local.enabled && var.publish_image_policy ? 1 : 0

  bucket                 = aws_s3_bucket.this[0].id
  key                    = local.platform_policy_key
  content                = var.image_policy
  content_type           = "application/yaml"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.this[0].arn

  depends_on = [
    aws_s3_bucket_versioning.this,
    aws_s3_bucket_server_side_encryption_configuration.this,
    aws_s3_bucket_policy.this,
  ]
}

resource "aws_iam_policy" "platform_exceptions_read" {
  count = local.enabled ? 1 : 0

  name        = "${var.name_prefix}-platform-exceptions-read"
  description = "Reads the platform image exceptions file, for the pull gate of every tenant worker."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadThePlatformExceptions"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${local.bucket_arn}/${local.platform_exceptions_key}"
      },
      {
        Sid       = "DecryptThroughS3"
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = local.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${local.region}.amazonaws.com" } }
      },
    ]
  })

  tags = var.tags
}
