data "aws_caller_identity" "current" {}

locals {
  enabled = var.create_evidence_store
}

resource "aws_s3_bucket" "evidence" {
  count = local.enabled ? 1 : 0

  bucket_prefix       = "${var.name_prefix}-evidence-"
  object_lock_enabled = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-evidence" })
}

resource "aws_s3_bucket_ownership_controls" "evidence" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.evidence[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "evidence" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.evidence[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "evidence" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.evidence[0].id
  rule {
    default_retention {
      mode = var.object_lock_mode
      days = var.retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.evidence]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.evidence[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == "" ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn == "" ? null : var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != ""
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  count = local.enabled ? 1 : 0

  bucket                  = aws_s3_bucket.evidence[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "evidence" {
  count = local.enabled ? 1 : 0

  bucket        = aws_s3_bucket.evidence[0].id
  target_bucket = var.access_log_bucket
  target_prefix = "${var.name_prefix}-evidence/"
}

resource "aws_s3_bucket_policy" "evidence" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.evidence[0].id
  policy = data.aws_iam_policy_document.evidence[0].json
}

data "aws_iam_policy_document" "evidence" {
  count = local.enabled ? 1 : 0

  statement {
    sid     = "DenyNonTls"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.evidence[0].arn,
      "${aws_s3_bucket.evidence[0].arn}/*",
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

  statement {
    sid       = "RestrictWritesToAllowlist"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence[0].arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values = concat(
        ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"],
        var.writer_principal_list,
      )
    }
  }
}
