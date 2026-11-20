data "aws_caller_identity" "current" {}

locals {
  enabled     = var.create_bucket
  bucket_name = var.bucket_name != "" ? var.bucket_name : "${var.name_prefix}-access-logs-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "this" {
  count = local.enabled ? 1 : 0

  bucket = local.bucket_name

  tags = merge(var.tags, { Name = local.bucket_name })
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

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
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

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id

  rule {
    id     = "tier-and-expire-access-logs"
    status = "Enabled"
    filter {}

    transition {
      days          = var.cold_storage_after_days
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = var.retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "this" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "S3ServerAccessLogsDelivery"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.this[0].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid     = "DenyNonTls"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.this[0].arn,
      "${aws_s3_bucket.this[0].arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  policy = data.aws_iam_policy_document.this[0].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}
