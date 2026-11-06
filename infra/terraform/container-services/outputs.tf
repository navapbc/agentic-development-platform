output "repository_urls" {
  description = "ECR repository of each image, keyed by the name inv publish-platform-images uses. Empty when disabled."
  value       = var.create_container_services ? { for name, repository in module.repository : name => repository.repository_url } : {}
}

output "internal_endpoints" {
  description = "The internal load balancer's client security group, the target security group and target group of each service, and each service's HTTPS URL. Null without the internal load balancer."
  value = local.internal_lb ? {
    client_security_group_id  = aws_security_group.internal_client[0].id
    target_security_group_ids = { for name, group in aws_security_group.internal_target : name => group.id }
    target_group_arns         = { for name, group in aws_lb_target_group.internal : name => group.arn }
    urls                      = { for name in keys(local.internal_lb_services) : name => "https://${name}.${var.internal_domain}" }
    domain                    = var.internal_domain
  } : null
}

output "execution_role_arns" {
  description = "Execution role of each platform service and one-shot task, which the secret store lists as a reader of the secrets it injects."
  value       = { for name, role in aws_iam_role.workload_execution : name => role.arn }
}

output "service_arns" {
  description = "ECS service of each platform service and worker slot, for secret_restart_services."
  value       = { for name, service in aws_ecs_service.platform : name => service.id }
}

output "workspace" {
  description = "Workspace file system and its checkouts and index access points, for the EKS workloads that write the checkouts. Null without the services."
  value = local.services_enabled ? {
    file_system_id    = aws_efs_file_system.workspace[0].id
    file_system_arn   = aws_efs_file_system.workspace[0].arn
    access_point_arns = { for name, point in aws_efs_access_point.workspace : name => point.arn }
    security_group_id = aws_security_group.workspace[0].id
  } : null
}

output "deploy" {
  description = "What inv platform-deploy runs. Null until the images are set."
  value = local.services_enabled ? {
    cluster = var.cluster_arn
    services = {
      for name in keys(local.services) : name => {
        service         = aws_ecs_service.platform[name].name
        task_definition = aws_ecs_task_definition.workload[name].arn
        count           = lookup(var.service_counts, name, 1)
      } if !contains(local.versioned_workers, name)
    }
    versioned_services = {
      for name in keys(local.services) : name => {
        slots           = { for slot in local.slots : slot => aws_ecs_service.platform["${name}-${slot}"].name }
        task_definition = aws_ecs_task_definition.workload[name].arn
        count           = lookup(var.service_counts, name, 1)
      } if contains(local.versioned_workers, name)
    }
    tasks = {
      for name in keys(local.tasks) : name => {
        task_definition = aws_ecs_task_definition.workload[name].arn
        container       = name
        security_groups = local.workload_security_groups[name]
      }
    }
    task_subnets = var.management_subnet_ids
  } : null
}

output "secret_readers" {
  description = "Execution roles that inject each secret, keyed by its secret store key, for the secret store's secret_readers. Empty without the services."
  value = local.services_enabled ? {
    database_platform_app      = [for name, spec in local.workloads : aws_iam_role.workload_execution[name].arn if spec.database && name != "db-migrate"]
    database_platform_migrator = [aws_iam_role.workload_execution["db-migrate"].arn]
    coordination_shared_secret = [aws_iam_role.workload_execution["coordination"].arn]
    review_signing_key         = [aws_iam_role.workload_execution["review-worker"].arn]
    review_attestation_secret  = [aws_iam_role.workload_execution["review-worker"].arn]
  } : {}
}
