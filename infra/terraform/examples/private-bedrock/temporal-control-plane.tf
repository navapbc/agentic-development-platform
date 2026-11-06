locals {
  temporal_deployed = (
    var.create_temporal_control_plane && var.temporal_server_image != "" && var.temporal_admin_tools_image != ""
  )
  temporal_database_readers = local.temporal_deployed ? [
    module.temporal_control_plane.execution_role_arns["server"],
    module.temporal_control_plane.execution_role_arns["admin_tools"],
  ] : []
  temporal_restart_services = local.temporal_deployed ? [module.temporal_control_plane.server_service_arn] : []

  temporal_secret_readers             = length(local.temporal_database_readers) == 0 ? {} : { database_temporal = local.temporal_database_readers }
  temporal_restart_services_by_secret = length(local.temporal_restart_services) == 0 ? {} : { database_temporal = local.temporal_restart_services }
}

module "temporal_control_plane" {
  source = "../../temporal-control-plane"

  create_temporal_control_plane = var.create_temporal_control_plane
  name_prefix                   = var.name_prefix
  frontend_acm_certificate_arn  = var.temporal_frontend_acm_certificate_arn
  internode_acm_certificate_arn = var.temporal_internode_acm_certificate_arn
  frontend_server_name          = var.temporal_frontend_server_name
  internode_server_name         = var.temporal_internode_server_name

  frontend_certificate_publicly_trusted = var.temporal_frontend_certificate_publicly_trusted
  ca_secret_readers                     = values(module.container_services.execution_role_arns)
  fips_140_mode                         = var.temporal_fips_140_mode
  secret_recovery_window_days           = var.secret_recovery_window_days

  cluster_arn                 = module.ecs_cluster.cluster_arn
  vpc_id                      = local.vpc_id
  application_subnet_ids      = local.subnet_ids
  management_subnet_ids       = local.management_subnet_ids
  endpoints_security_group_id = local.endpoints_security_group_id

  database_host               = coalesce(module.aurora_postgresql.endpoint, "")
  database_cidr_blocks        = local.data_cidrs
  database_secret_arn         = lookup(module.secret_store.database_secret_arns, "temporal", "")
  database_secret_kms_key_arn = coalesce(module.secret_store.kms_key_arn, "")
  database_port               = 5432

  server_image      = var.temporal_server_image
  admin_tools_image = var.temporal_admin_tools_image
  ui_image          = var.temporal_ui_image
  server_count      = var.temporal_server_count

  ui_acm_certificate_arn         = var.temporal_ui_acm_certificate_arn
  ui_allowed_cidr_blocks         = var.temporal_ui_allowed_cidr_blocks
  ui_operator_security_group_ids = [module.bedrock_jumphost.task_security_group_id]

  access_log_bucket      = var.access_log_bucket
  enable_execute_command = var.temporal_enable_execute_command
  exec_task_policy_arn   = module.ecs_cluster.exec_task_policy_arn

  tags = var.tags
}
