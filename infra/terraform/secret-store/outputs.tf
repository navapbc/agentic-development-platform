output "kms_key_arn" {
  description = "KMS key that encrypts the secrets, or null when the module is disabled. Pass it to the elasticache module as secret_kms_key_arn so the cache secret uses the same key."
  value       = try(aws_kms_key.this[0].arn, null)
}

output "secret_arns" {
  description = "Secret ARNs keyed by secret key, for the valueFrom of an ECS task secret."
  value       = local.managed_secret_arns
}

output "database_secret_arns" {
  description = "Per-service database credential secret ARNs keyed by role. Each holds username and password."
  value       = { for role, key in local.database_secret_keys : role => aws_secretsmanager_secret.this[key].arn }
}

output "role_bootstrap_function_name" {
  description = "Function that creates the per-service database roles and databases when invoked with {\"action\": \"bootstrap\"}, or null when database credentials are disabled. Run it with inv db-bootstrap-roles."
  value       = try(aws_lambda_function.rotation["postgres"].function_name, null)
}

output "rotation_function_arns" {
  description = "Rotation function ARNs keyed by target, postgres and elasticache."
  value       = { for key, function in aws_lambda_function.rotation : key => function.arn }
}

output "restart_document_name" {
  description = "SSM Automation document that redeploys an ECS service, or null when no restart_services are set."
  value       = try(aws_ssm_document.restart[0].name, null)
}
