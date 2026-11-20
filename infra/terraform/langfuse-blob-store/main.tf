locals {
  enabled = var.create_bucket
}

resource "aws_s3_bucket" "this" {
  count = local.enabled ? 1 : 0

  bucket_prefix = "${var.name_prefix}-langfuse-"

  tags = merge(var.tags, { Name = "${var.name_prefix}-langfuse" })
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
    id     = "expire-noncurrent-and-abandoned-uploads"
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
      sse_algorithm     = var.kms_key_arn == "" ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn == "" ? null : var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != ""
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
  target_prefix = "${var.name_prefix}-langfuse/"
}

resource "aws_s3_bucket_policy" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  policy = data.aws_iam_policy_document.this[0].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}

data "aws_iam_policy_document" "this" {
  count = local.enabled ? 1 : 0

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
