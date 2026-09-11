output "cluster_name" {
  description = "ECS cluster name, or null when disabled."
  value       = try(aws_ecs_cluster.this[0].name, null)
}

output "task_definition_family" {
  description = "Task definition family the connect script runs, or null when disabled."
  value       = try(aws_ecs_task_definition.this[0].family, null)
}

output "task_security_group_id" {
  description = "Security group id of the Fargate task, or null when disabled."
  value       = try(aws_security_group.task[0].id, null)
}

output "exec_log_bucket" {
  description = "S3 bucket holding ECS Exec session logs, or null when disabled."
  value       = try(aws_s3_bucket.exec_logs[0].id, null)
}

output "ssm_param_prefix" {
  description = "SSM parameter prefix the connect script resolves, or null when disabled."
  value       = local.enabled ? local.param_prefix : null
}
