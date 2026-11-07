output "role_arn" {
  description = "The role the publish-images workflow assumes. Set it as the AWS_PUBLISH_ROLE_ARN repository variable. Null when disabled."
  value       = local.enabled ? aws_iam_role.publisher[0].arn : null
}

output "oidc_provider_arn" {
  description = "The GitHub OIDC provider the role trusts. Null when disabled."
  value       = local.enabled ? local.provider_arn : null
}
