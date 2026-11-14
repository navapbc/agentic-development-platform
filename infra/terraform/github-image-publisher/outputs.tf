output "role_arn" {
  description = "The role the publish-images workflow assumes. Set it as the AWS_PUBLISH_ROLE_ARN repository variable. Null when disabled."
  value       = local.enabled ? aws_iam_role.publisher[0].arn : null
}

output "oidc_provider_arn" {
  description = "The GitHub OIDC provider the role trusts. Null when disabled."
  value       = local.enabled ? local.provider_arn : null
}

output "exception_role_arn" {
  description = "The role the image-exception workflow assumes. Set it as the AWS_IMAGE_EXCEPTION_ROLE_ARN repository variable. Null without config_store."
  value       = local.exceptions_enabled ? aws_iam_role.exception[0].arn : null
}
