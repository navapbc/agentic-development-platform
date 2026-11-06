locals {
  container_service_arns = module.container_services.service_arns
  off_hours_foundation = compact(concat(
    [module.temporal_control_plane.server_service_arn, module.temporal_control_plane.ui_service_arn],
    [for name in ["clickhouse", "otel-collector"] : lookup(local.container_service_arns, name, "")],
  ))
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
