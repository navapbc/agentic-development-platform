output "cluster_arn" {
  description = "ARN of the platform ECS cluster."
  value       = local.enabled ? aws_ecs_cluster.this[0].arn : ""
}

output "cluster_name" {
  description = "Name of the platform ECS cluster."
  value       = local.enabled ? aws_ecs_cluster.this[0].name : ""
}

output "logs_kms_key_arn" {
  description = "KMS key that encrypts ECS Exec sessions and the cluster log groups. An operator who opens an Exec session needs kms:GenerateDataKey on it."
  value       = local.enabled ? aws_kms_key.logs[0].arn : ""
}

output "storage_kms_key_arn" {
  description = "KMS key that encrypts Fargate ephemeral storage and managed storage in the cluster."
  value       = local.enabled ? aws_kms_key.storage[0].arn : ""
}

output "exec_log_group_name" {
  description = "CloudWatch log group that records ECS Exec session output."
  value       = local.enabled ? aws_cloudwatch_log_group.exec[0].name : ""
}

output "deployer_policy_arn" {
  description = "IAM policy a team attaches to its deploy role to register task definitions and manage services and tasks in this cluster only."
  value       = local.enabled ? aws_iam_policy.deployer[0].arn : ""
}

output "exec_task_policy_arn" {
  description = "IAM policy a task role needs to accept ECS Exec sessions in this cluster."
  value       = local.enabled ? aws_iam_policy.exec_task[0].arn : ""
}
