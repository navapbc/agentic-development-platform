output "bucket" {
  description = "Bucket name. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].id, null)
}

output "bucket_arn" {
  description = "Bucket ARN. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].arn, null)
}

output "kms_key_arn" {
  description = "KMS key that encrypts the bucket. Null when the module is disabled."
  value       = try(aws_kms_key.this[0].arn, null)
}

output "tenant_prefixes" {
  description = "Key prefix of each tenant's bundle."
  value       = { for tenant in keys(var.tenants) : tenant => "tenants/${tenant}/" }
}
