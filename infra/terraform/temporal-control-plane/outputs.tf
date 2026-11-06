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

output "ui_repository_url" {
  description = "ECR repository for the UI image that docker/temporal-ui.Dockerfile builds, or null when disabled."
  value       = module.ui_repository.repository_url
}

output "endpoint" {
  description = "host:port the Temporal clients dial, the frontend NLB. Empty until the images are set."
  value       = local.endpoint
}

output "namespace" {
  description = "Namespace the workers use."
  value       = var.namespace
}

output "frontend_server_name" {
  description = "Name clients verify the frontend certificate against, their TEMPORAL_TLS_SERVER_NAME."
  value       = var.frontend_server_name
}

output "internode_server_name" {
  description = "Name on the internode certificate, which the server tasks verify each other against."
  value       = var.internode_server_name
}

output "client_ca_secret_arn" {
  description = "Secret clients inject as TEMPORAL_TLS_CA. Null when the frontend chains to a public root."
  value       = local.client_ca ? try(aws_secretsmanager_secret.tls["ca"].arn, null) : null
}

output "tls_kms_key_arn" {
  description = "KMS key that encrypts the TLS secrets, which a client decrypts to read the CA secret. Null when disabled."
  value       = try(aws_kms_key.temporal[0].arn, null)
}

output "client_security_group_id" {
  description = "Security group a workload attaches to reach the frontend. Null until the images are set."
  value       = try(aws_security_group.client[0].id, null)
}

output "server_service_arn" {
  description = "Server ECS service, for secret_restart_services after a temporal role rotation. Null until the images are set."
  value       = try(aws_ecs_service.server[0].id, null)
}

output "execution_role_arns" {
  description = "Execution role of each task, keyed by server, admin_tools, and ui."
  value       = { for name, role in aws_iam_role.execution : name => role.arn }
}

output "ui_url" {
  description = "HTTPS address of the UI load balancer. Null without the UI."
  value       = try("https://${aws_lb.ui[0].dns_name}", null)
}

output "deploy" {
  description = "What inv temporal-deploy runs. Null until the images are set."
  value = local.deploy ? {
    cluster                     = var.cluster_arn
    server_service              = aws_ecs_service.server[0].name
    server_task_definition      = aws_ecs_task_definition.server[0].arn
    server_count                = var.server_count
    admin_tools_task_definition = aws_ecs_task_definition.admin_tools[0].arn
    admin_tools_subnets         = var.management_subnet_ids
    admin_tools_security_groups = [aws_security_group.admin_tools[0].id, aws_security_group.client[0].id]
    ui_service                  = local.ui ? aws_ecs_service.ui[0].name : null
  } : null
}
