output "replication_group_id" {
  description = "Replication group id, or null when the module is disabled."
  value       = try(aws_elasticache_replication_group.this[0].id, null)
}

output "primary_endpoint_address" {
  description = "Primary endpoint. Clients connect over TLS, so Langfuse sets REDIS_TLS_ENABLED=true."
  value       = try(aws_elasticache_replication_group.this[0].primary_endpoint_address, null)
}

output "reader_endpoint_address" {
  description = "Reader endpoint."
  value       = try(aws_elasticache_replication_group.this[0].reader_endpoint_address, null)
}

output "port" {
  description = "Cache port."
  value       = var.port
}

output "username" {
  description = "RBAC user the client authenticates as, set as REDIS_USERNAME."
  value       = var.username
}

output "user_arn" {
  description = "ARN of the RBAC user."
  value       = try(aws_elasticache_user.client[0].arn, null)
}

output "user_secret_arn" {
  description = "Secrets Manager ARN holding the RBAC user credential. Its password is injected as REDIS_AUTH, and the secret store feature attaches rotation to it."
  value       = try(aws_secretsmanager_secret.user[0].arn, null)
}

output "security_group_id" {
  description = "Cache security group id, or null when the module is disabled."
  value       = try(aws_security_group.this[0].id, null)
}
