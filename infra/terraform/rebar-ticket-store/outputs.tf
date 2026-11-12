output "bucket" {
  description = "Bucket name, set as store.bucket in tickets.yaml. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].id, null)
}

output "bucket_arn" {
  description = "Bucket ARN. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].arn, null)
}

output "prefix" {
  description = "Key prefix of the store, set as store.prefix in tickets.yaml."
  value       = var.prefix
}

output "reader_role_arn" {
  description = "Role that fetches the store, set as roles.read_only in tickets.yaml and assumed by the reader profile. Null when the module is disabled."
  value       = local.enabled ? local.reader_role_arn : null
}

output "writer_role_arn" {
  description = "Role that pushes the store, set as roles.write in tickets.yaml and assumed by the writer profile. Null when the module is disabled."
  value       = local.enabled ? local.writer_role_arn : null
}

output "kms_key_arn" {
  description = "KMS key that encrypts the store. Null when the module is disabled."
  value       = try(aws_kms_key.this[0].arn, null)
}
