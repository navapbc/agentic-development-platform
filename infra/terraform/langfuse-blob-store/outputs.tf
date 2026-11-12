output "bucket_id" {
  description = "Bucket name, set as LANGFUSE_S3_EVENT_UPLOAD_BUCKET and LANGFUSE_S3_MEDIA_UPLOAD_BUCKET. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].id, null)
}

output "bucket_arn" {
  description = "Bucket ARN for the Langfuse task role policy. Null when the module is disabled."
  value       = try(aws_s3_bucket.this[0].arn, null)
}
