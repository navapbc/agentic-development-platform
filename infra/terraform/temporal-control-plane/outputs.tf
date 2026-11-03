output "server_repository_url" {
  description = "ECR repository for the server image that docker/temporal.Dockerfile builds, or null when disabled."
  value       = module.server_repository.repository_url
}

output "server_repository_name" {
  description = "Name of the server image repository, or null when disabled."
  value       = module.server_repository.repository_name
}

output "admin_tools_repository_url" {
  description = "ECR repository for the admin-tools image that docker/temporal-admin-tools.Dockerfile builds, or null when disabled."
  value       = module.admin_tools_repository.repository_url
}

output "admin_tools_repository_name" {
  description = "Name of the admin-tools image repository, or null when disabled."
  value       = module.admin_tools_repository.repository_name
}

output "frontend_certificate_source" {
  description = "Where the frontend certificate clients see comes from, acm when the NLB presents frontend_acm_certificate_arn, or secrets."
  value       = local.frontend_from_secrets ? "secrets" : "acm"
}

output "tls_ca_secret_arn" {
  description = "Secret holding the CA bundle, which clients trust when the frontend certificate comes from Secrets Manager. Null when disabled."
  value       = try(aws_secretsmanager_secret.tls["ca"].arn, null)
}

output "tls_internode_secret_arn" {
  description = "Secret holding the internode certificate and key. Null when disabled."
  value       = try(aws_secretsmanager_secret.tls["internode"].arn, null)
}

output "tls_frontend_secret_arn" {
  description = "Secret holding the frontend certificate and key. Null when disabled or when the NLB presents an ACM certificate."
  value       = try(aws_secretsmanager_secret.tls["frontend"].arn, null)
}

output "frontend_acm_certificate_arn" {
  description = "ACM certificate the NLB presents, or empty."
  value       = var.frontend_acm_certificate_arn
}

output "internode_acm_certificate_arn" {
  description = "ACM certificate that tracks the internode certificate, or empty."
  value       = var.internode_acm_certificate_arn
}

output "certificate_expiry_topic_arn" {
  description = "SNS topic that receives the ACM expiry events of the tracked certificates. Subscribe the operators to it. Null when no ACM certificate is tracked."
  value       = try(aws_sns_topic.certificate_expiry[0].arn, null)
}
