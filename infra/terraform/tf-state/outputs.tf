output "bucket_id" {
  description = "Name of the state bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.state[0].id, null)
}

output "bucket_arn" {
  description = "ARN of the state bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.state[0].arn, null)
}
