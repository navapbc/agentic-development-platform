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
  enabled        = var.create_rebar_ticket_store
  supplied_roles = var.existing_reader_role_arn != "" || var.existing_writer_role_arn != ""
  create_roles   = local.enabled && !local.supplied_roles

  account_id = try(data.aws_caller_identity.current[0].account_id, "")
  partition  = try(data.aws_partition.current[0].partition, "aws")
  region     = try(data.aws_region.current[0].region, "")

  reader_role_arn = local.create_roles ? aws_iam_role.reader[0].arn : var.existing_reader_role_arn
  writer_role_arn = local.create_roles ? aws_iam_role.writer[0].arn : var.existing_writer_role_arn

  bucket_arn     = try(aws_s3_bucket.this[0].arn, "")
  kms_key_arn    = try(aws_kms_key.this[0].arn, "")
  objects_arn    = "${local.bucket_arn}/${var.prefix}/*"
  object_readers = concat([local.reader_role_arn, local.writer_role_arn], var.admin_principals)
  object_writers = concat([local.writer_role_arn], var.admin_principals)
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.supplied_roles || (var.existing_reader_role_arn != "" && var.existing_writer_role_arn != "")
      error_message = "Set existing_reader_role_arn and existing_writer_role_arn together, or neither so the module creates both roles."
    }
    precondition {
      condition     = !local.supplied_roles || var.existing_reader_role_arn != var.existing_writer_role_arn
      error_message = "existing_reader_role_arn and existing_writer_role_arn must name different roles."
    }
    precondition {
      condition     = !local.create_roles || length(var.trusted_principals) > 0
      error_message = "trusted_principals must name at least one principal when the module creates the reader and writer roles."
    }
  }
}

resource "aws_kms_key" "this" {
  count = local.enabled ? 1 : 0

  description             = "Encrypts the ${var.name_prefix} rebar ticket store bucket."
  policy                  = local.key_policy
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(var.tags, { Name = "${var.name_prefix}-rebar-tickets" })
}

resource "aws_kms_alias" "this" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-rebar-tickets"
  target_key_id = aws_kms_key.this[0].id
}

resource "aws_s3_bucket" "this" {
  count = local.enabled ? 1 : 0

  bucket_prefix = "${var.name_prefix}-rebar-tickets-"

  tags = merge(var.tags, { Name = "${var.name_prefix}-rebar-tickets" })
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
    id     = "expire-replaced-bundles-and-abandoned-uploads"
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
  target_prefix = "${var.name_prefix}-rebar-tickets/"
}

resource "aws_s3_bucket_policy" "this" {
  count = local.enabled ? 1 : 0

  bucket = aws_s3_bucket.this[0].id
  policy = local.bucket_policy

  depends_on = [aws_s3_bucket_public_access_block.this]
}
