output "repository_url" {
  description = "Repository URL to push and pull images, or null when disabled."
  value       = try(aws_ecr_repository.this[0].repository_url, null)
}

output "repository_arn" {
  description = "Repository ARN, or null when disabled."
  value       = try(aws_ecr_repository.this[0].arn, null)
}

output "kms_key_arn" {
  description = "ARN of the KMS key encrypting the repository, or null when disabled."
  value       = local.enabled ? local.kms_key_arn : null
}

output "repository_name" {
  description = "Repository name, or null when disabled."
  value       = try(aws_ecr_repository.this[0].name, null)
}
