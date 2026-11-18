output "task_role_arn" {
  description = "Role the task presents to the EKS API, for an access entry. Empty when disabled."
  value       = local.enabled ? aws_iam_role.task[0].arn : ""
}

output "execution_role_arn" {
  description = "Execution role of the task, which a caller that runs the task passes. Empty when disabled."
  value       = local.enabled ? aws_iam_role.execution[0].arn : ""
}

output "security_group_id" {
  description = "Security group of the task, for the EKS API's admitted groups. Empty when disabled."
  value       = local.enabled ? aws_security_group.task[0].id : ""
}

output "log_group_name" {
  description = "Log group the task writes to. Empty when disabled."
  value       = local.enabled ? aws_cloudwatch_log_group.task[0].name : ""
}

output "family" {
  description = "Task definition family, which runs the latest revision. Empty without the image."
  value       = local.deploy ? local.family : ""
}

output "container" {
  description = "Name of the deployer container, for a command override."
  value       = local.container
}

output "deploy" {
  description = "Cluster, subnets, task definition, container, and security group inv eks-deploy runs the task with. Null without the image."
  value = local.deploy ? {
    cluster      = var.cluster_arn
    task_subnets = var.subnet_ids
    log_group    = aws_cloudwatch_log_group.task[0].name
    tasks = {
      eks-deployer = {
        task_definition = aws_ecs_task_definition.deployer[0].arn
        container       = local.container
        security_groups = [aws_security_group.task[0].id]
      }
    }
  } : null
}
