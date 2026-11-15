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
  value       = { for tenant in var.tenants : tenant => "tenants/${tenant}/" }
}

output "tenant_principal_tag_keys" {
  description = "Principal tag keys whose value, a tenant name, admits the principal to that tenant's prefix. A principal tagged with the reader key reads tenants/<tenant>/, and one tagged with the publisher key reads, writes, and deletes it. Grant iam:TagRole on these keys to administrators only, and let no caller pass them as session tags to a role that reads the bucket."
  value       = local.tenant_tag_keys
}

output "platform_exceptions_key" {
  description = "Key of the platform image exceptions file."
  value       = local.platform_exceptions_key
}

output "platform_policy_key" {
  description = "Key of the image policy the exception writers check scope against."
  value       = local.platform_policy_key
}

output "platform_exceptions_read_policy_arn" {
  description = "IAM policy that reads the platform image exceptions file, for each tenant worker. Null when the module is disabled."
  value       = try(aws_iam_policy.platform_exceptions_read[0].arn, null)
}
