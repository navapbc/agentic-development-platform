module "access_logs" {
  source = "../access-logs"

  create_bucket = var.create_access_log_bucket
  bucket_name   = var.access_log_bucket
  name_prefix   = var.name_prefix
  tags          = var.tags
}

locals {
  access_log_bucket = var.create_access_log_bucket ? module.access_logs.bucket_id : var.access_log_bucket
}

module "tf_state" {
  source = "../tf-state"

  create_state_bucket = true
  name_prefix         = var.name_prefix
  kms_key_arn         = var.kms_key_arn
  access_log_bucket   = local.access_log_bucket
  tags                = var.tags
}
