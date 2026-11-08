locals {
  container_service_arns = module.container_services.service_arns
  off_hours_foundation = concat(
    module.temporal_control_plane.service_arns,
    [for name in ["clickhouse", "otel-collector"] : local.container_service_arns[name] if contains(keys(local.container_service_arns), name)],
  )
}

module "off_hours" {
  source = "../../off-hours"

  create_off_hours_schedule = var.off_hours_enabled
  name_prefix               = var.name_prefix
  region                    = var.region
  cluster_arns              = compact([module.ecs_cluster.cluster_arn])
  foundation_services       = local.off_hours_foundation
  services                  = [for name, arn in local.container_service_arns : arn if !contains(["clickhouse", "otel-collector"], name)]
  timezone                  = var.off_hours_timezone
  sleep_schedule            = var.off_hours_sleep_schedule
  wake_schedule             = var.off_hours_wake_schedule

  tags = var.tags
}
