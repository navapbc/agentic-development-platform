data "aws_elb_service_account" "this" {
  count = local.deploy ? 1 : 0
}

locals {
  lb_log_bucket = "${var.name_prefix}-temporal-lb-logs-${local.account_id}"
  lb_log_arn    = "arn:${local.partition}:s3:::${local.lb_log_bucket}"

  lb_log_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTls"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.lb_log_arn, "${local.lb_log_arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "AllowAlbLogDelivery"
        Effect    = "Allow"
        Principal = { AWS = try(data.aws_elb_service_account.this[0].arn, "") }
        Action    = "s3:PutObject"
        Resource  = "${local.lb_log_arn}/ui/AWSLogs/${local.account_id}/*"
      },
      {
        Sid       = "AllowNlbLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${local.lb_log_arn}/frontend/AWSLogs/${local.account_id}/*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
      {
        Sid       = "AllowNlbLogDeliveryAclCheck"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = local.lb_log_arn
        Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
      },
    ]
  })
}

resource "aws_s3_bucket" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket        = local.lb_log_bucket
  force_destroy = true

  tags = merge(var.tags, { Name = local.lb_log_bucket })
}

resource "aws_s3_bucket_ownership_controls" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket = aws_s3_bucket.lb_logs[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket = aws_s3_bucket.lb_logs[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# Load balancer log delivery writes only to buckets encrypted with S3-managed keys.
resource "aws_s3_bucket_server_side_encryption_configuration" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket = aws_s3_bucket.lb_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket                  = aws_s3_bucket.lb_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket = aws_s3_bucket.lb_logs[0].id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.load_balancer_log_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_logging" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket        = aws_s3_bucket.lb_logs[0].id
  target_bucket = var.access_log_bucket
  target_prefix = "${local.lb_log_bucket}/"
}

resource "aws_s3_bucket_policy" "lb_logs" {
  count = local.deploy ? 1 : 0

  bucket = aws_s3_bucket.lb_logs[0].id
  policy = local.lb_log_policy

  depends_on = [aws_s3_bucket_public_access_block.lb_logs]
}
