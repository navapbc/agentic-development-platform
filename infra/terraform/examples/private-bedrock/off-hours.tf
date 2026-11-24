locals {
  container_service_arns = module.container_services.service_arns
  off_hours_foundation = concat(
    module.temporal_control_plane.service_arns,
    [for name in ["clickhouse", "otel-collector"] : local.container_service_arns[name] if contains(keys(local.container_service_arns), name)],
  )

  # The EKS deployments that hold an Aurora connection.
  off_hours_eks_deployer = local.eks_deployer_enabled && var.eks_deployer_image != "" ? {
    cluster_arn        = module.ecs_cluster.cluster_arn
    task_family        = module.eks_deployer.family
    container          = module.eks_deployer.container
    subnet_ids         = local.management_subnet_ids
    security_group_ids = [module.eks_deployer.security_group_id]
    pass_role_arns     = [module.eks_deployer.execution_role_arn, module.eks_deployer.task_role_arn]
    log_group_name     = module.eks_deployer.log_group_name
    deployments        = ["tenant-workers", "tenant-review-workers", "mcp-gateway", "inbound-webhook", "litellm"]
  } : null

  off_hours_database = local.secret_store_database ? {
    cluster_arn        = module.aurora_postgresql.cluster_arn
    monitor_secret_arn = module.secret_store.secret_arns["database_monitor"]
  } : null
  off_hours_secret_readers = var.off_hours_enabled && local.secret_store_database ? { database_monitor = compact([module.off_hours.function_role_arn]) } : {}
}

module "off_hours" {
  source = "../../off-hours"

  create_off_hours_schedule = var.off_hours_enabled
  name_prefix               = var.name_prefix
  region                    = var.region
  cluster_arns              = compact([module.ecs_cluster.cluster_arn])
  foundation_services       = local.off_hours_foundation
  services                  = [for name, arn in local.container_service_arns : arn if !contains(["clickhouse", "otel-collector"], name)]
  health_checked_services   = [for arn in module.temporal_control_plane.service_arns : arn if endswith(arn, "/${var.name_prefix}-temporal-server")]
  eks_deployer              = local.off_hours_eks_deployer
  database                  = local.off_hours_database
  foundation_database_users = local.secret_store_database ? ["temporal"] : []
  timezone                  = var.off_hours_timezone
  sleep_schedule            = var.off_hours_sleep_schedule
  wake_schedule             = var.off_hours_wake_schedule

  tags = var.tags
}
