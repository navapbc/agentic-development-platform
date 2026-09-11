resource "aws_s3_bucket" "exec_logs" {
  count = local.enabled ? 1 : 0

  bucket_prefix = substr("${local.task_label}-exec-", 0, 37)
  force_destroy = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-jumphost-exec-logs" })
}

resource "aws_s3_bucket_versioning" "exec_logs" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.exec_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exec_logs" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.exec_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.logs_bucket_kms_key_arn == "" ? "AES256" : "aws:kms"
      kms_master_key_id = var.logs_bucket_kms_key_arn == "" ? null : var.logs_bucket_kms_key_arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "exec_logs" {
  count = local.enabled ? 1 : 0

  bucket                  = aws_s3_bucket.exec_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "exec_logs" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.exec_logs[0].id
  policy = data.aws_iam_policy_document.exec_logs[0].json
}

data "aws_iam_policy_document" "exec_logs" {
  count = local.enabled ? 1 : 0

  statement {
    sid     = "DenyNonTls"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.exec_logs[0].arn,
      "${aws_s3_bucket.exec_logs[0].arn}/*",
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
    sid       = "RestrictObjectReadsToAllowlist"
    effect    = "Deny"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.exec_logs[0].arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values = concat(
        ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"],
        var.logs_bucket_principal_list,
      )
    }
  }
}
