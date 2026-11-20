output "bucket_id" {
  description = "Name of the evidence bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.evidence[0].id, null)
}

output "bucket_arn" {
  description = "ARN of the evidence bucket, or null when the module is disabled."
  value       = try(aws_s3_bucket.evidence[0].arn, null)
}
