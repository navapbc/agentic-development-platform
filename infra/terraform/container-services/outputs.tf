output "repository_urls" {
  description = "ECR repository of each image, keyed by the name inv publish-platform-images uses. Empty when disabled."
  value       = var.create_container_services ? { for name, repository in module.repository : name => repository.repository_url } : {}
}

output "repository_arns" {
  description = "ARN of each image's ECR repository, keyed as repository_urls. Empty when disabled."
  value       = var.create_container_services ? { for name, repository in module.repository : name => repository.repository_arn } : {}
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

output "temporal_client_role_arns" {
  description = "Execution roles of the workloads that connect to Temporal, which inject the Temporal CA."
  value       = [for name, spec in local.all_workloads : aws_iam_role.workload_execution[name].arn if spec.temporal]
}

output "service_arns" {
  description = "ECS service of each platform service and worker slot, for secret_restart_services."
  value = merge(
    { for name, service in aws_ecs_service.platform : name => service.id },
    { for name, service in aws_ecs_service.langfuse : name => service.id },
    { for name, service in aws_ecs_service.collector : name => service.id },
  )
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
    services = merge(
      {
        for name in keys(local.services) : name => {
          service         = aws_ecs_service.platform[name].name
          task_definition = aws_ecs_task_definition.workload[name].arn
          count           = lookup(var.service_counts, name, 1)
        } if !contains(local.versioned_workers, name)
      },
      {
        for name in keys(local.collector_workloads) : name => {
          service         = aws_ecs_service.collector[name].name
          task_definition = aws_ecs_task_definition.collector[name].arn
          count           = lookup(var.service_counts, name, 1)
        }
      },
    )
    langfuse_services = {
      for name in keys(local.langfuse_services) : name => {
        service         = aws_ecs_service.langfuse[name].name
        task_definition = aws_ecs_task_definition.langfuse[name].arn
        count           = lookup(var.service_counts, name, 1)
      }
    }
    versioned_services = {
      for name in keys(local.services) : name => {
        slots           = { for slot in local.slots : slot => aws_ecs_service.platform["${name}-${slot}"].name }
        task_definition = aws_ecs_task_definition.workload[name].arn
        count           = lookup(var.service_counts, name, 1)
      } if contains(local.versioned_workers, name)
    }
    tasks = merge(
      {
        for name in keys(local.tasks) : name => {
          task_definition = aws_ecs_task_definition.workload[name].arn
          container       = name
          security_groups = local.workload_security_groups[name]
        }
      },
      {
        for name, spec in local.langfuse_workloads : name => {
          task_definition = aws_ecs_task_definition.langfuse[name].arn
          container       = name
          security_groups = local.workload_security_groups[name]
        } if spec.one_shot
      },
    )
    task_subnets = var.management_subnet_ids
  } : null
}

output "platform_environment" {
  description = "Environment every platform task receives, without the config files, for the EKS workloads that run the platform image. Null without the services."
  value       = local.services_enabled ? { for key, value in local.platform_environment : key => value if key != "PLATFORM_ENV_CONFIG" && value != "" } : null
}

output "secret_readers" {
  description = "Execution roles that inject each secret, keyed by its secret store key, for the secret store's secret_readers. Empty without the services."
  value = local.services_enabled ? merge(
    {
      for key in local.store_secret_keys : key => [
        for name in keys(local.all_workloads) : aws_iam_role.workload_execution[name].arn if contains(local.workload_secret_names[name], key)
      ]
    },
    {
      database_platform_app      = [for name, spec in local.workloads : aws_iam_role.workload_execution[name].arn if spec.database && name != "db-migrate"]
      database_platform_migrator = [aws_iam_role.workload_execution["db-migrate"].arn]
    },
  ) : {}
}

output "secret_restart_services" {
  description = "ECS services and worker slots that read each secret, keyed by its secret store key, for the secret store's restart_services."
  value = {
    for key in local.restart_secret_keys : key => [
      for instance in local.restart_instances : instance.id if contains(local.workload_restart_keys[instance.workload], key)
    ] if anytrue([for instance in local.restart_instances : contains(local.workload_restart_keys[instance.workload], key)])
  }
}

output "telemetry" {
  description = "The Prometheus workspace the collector writes to, and whether the module sets the region's X-Ray encryption. Null without collector exports."
  value = local.collector_exports ? {
    prometheus_workspace_id = aws_prometheus_workspace.telemetry[0].id
    prometheus_endpoint     = aws_prometheus_workspace.telemetry[0].prometheus_endpoint
    xray_encryption_key_arn = var.xray_encryption_enabled ? aws_kms_key.telemetry[0].arn : null
    collector_url           = local.collector_url
  } : null
}

output "clickhouse" {
  description = "The ClickHouse TLS secret inv clickhouse-certs put writes, and the name its certificate must carry. Null without Langfuse."
  value = local.langfuse_enabled ? {
    tls_secret_arn = aws_secretsmanager_secret.clickhouse_tls[0].arn
    server_name    = local.clickhouse_host
  } : null
}

output "langfuse" {
  description = "The internal Langfuse URL the platform services export to, and the URL operators open through the jumphost. Null without Langfuse."
  value = local.langfuse_enabled ? {
    url        = "https://langfuse.${var.internal_domain}"
    public_url = local.lf.public_url
  } : null
}
