output "state_bucket" {
  description = "Bucket to set as `bucket` in each composition root's backend.hcl."
  value       = module.tf_state.bucket_id
}

output "access_log_bucket" {
  description = "Bucket that receives S3 server access logs. Set it as `access_log_bucket` in each composition root's tfvars."
  value       = local.access_log_bucket
}

output "backend_hcl" {
  description = "Backend settings for the private-bedrock composition root. Write this to its backend.hcl."
  value = join("\n", compact([
    "bucket       = \"${module.tf_state.bucket_id}\"",
    "key          = \"private-bedrock/terraform.tfstate\"",
    "region       = \"${var.region}\"",
    "encrypt      = true",
    "use_lockfile = true",
    var.kms_key_arn == "" ? "" : "kms_key_id   = \"${var.kms_key_arn}\"",
  ]))
}
