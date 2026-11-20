output "bucket_id" {
  description = "Name of the access-log bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].id, null)
}

output "bucket_arn" {
  description = "ARN of the access-log bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].arn, null)
}
