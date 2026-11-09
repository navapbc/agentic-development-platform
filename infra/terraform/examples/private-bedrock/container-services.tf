locals {
  # DNS Firewall already resolves *.internal. Any other internal domain is added to its allowlist.
  container_internal_domains = (
    var.create_container_services && var.container_internal_acm_certificate_arn != "" && !endswith(var.container_internal_domain, ".internal")
    ? ["*.${var.container_internal_domain}"] : []
  )
  container_environment_config = var.container_config_dir == "" ? {} : {
    for name in fileset(var.container_config_dir, "*.yaml") : name => file("${var.container_config_dir}/${name}")
  }

  reader_sets = [var.secret_readers, local.temporal_secret_readers, module.container_services.secret_readers, local.eks_secret_readers, local.tenant_secret_readers, local.tenant_platform_secret_readers]
  secret_readers = {
    for key in distinct(flatten([for readers in local.reader_sets : keys(readers)])) :
    key => distinct(flatten([for readers in local.reader_sets : lookup(readers, key, [])]))
  }
  restart_sets = [var.secret_restart_services, local.temporal_restart_services_by_secret, module.container_services.secret_restart_services]

  container_telemetry_exports = var.create_container_services && var.container_collector_image != "" && var.container_collector_exports_enabled

  container_langfuse = (
    var.container_langfuse_web_image != "" && var.container_langfuse_worker_image != "" && var.container_clickhouse_image != "" &&
    var.container_tls_sidecar_image != "" && var.create_cache && var.create_clickhouse_storage && var.create_langfuse_bucket && var.create_secret_store
  )
  # The services of each set differ, and distinct() over service IDs not yet known has no known
  # length, which the secret store needs for its restart target keys.
  secret_restart_services = {
    for key in distinct(flatten([for services in local.restart_sets : keys(services)])) :
    key => flatten([for services in local.restart_sets : lookup(services, key, [])])
  }
}

module "container_services" {
  source = "../../container-services"

  create_container_services = var.create_container_services
  name_prefix               = var.name_prefix
  tenant_workspaces         = keys(local.tenants)
  platform_settings         = var.container_platform_settings

  vpc_id                       = local.vpc_id
  application_subnet_ids       = local.subnet_ids
  management_subnet_ids        = local.management_subnet_ids
  data_subnet_ids              = local.data_subnet_ids
  endpoints_security_group_id  = local.endpoints_security_group_id
  internal_acm_certificate_arn = var.container_internal_acm_certificate_arn
  internal_domain              = var.container_internal_domain
  internal_allowed_cidr_blocks = var.container_internal_allowed_cidr_blocks
  access_log_bucket            = var.access_log_bucket

  cluster_arn             = module.ecs_cluster.cluster_arn
  platform_image          = var.container_platform_image
  repo_intelligence_image = var.container_repo_intelligence_image
  environment             = var.container_environment
  environment_config      = local.container_environment_config
  platform_ca_pem         = var.container_platform_ca_file == "" ? "" : file(var.container_platform_ca_file)
  service_counts          = var.container_service_counts
  use_fips_endpoints      = var.container_use_fips_endpoints
  https_proxy             = var.container_https_proxy

  database_host                = coalesce(module.aurora_postgresql.endpoint, "")
  database_cidr_blocks         = local.data_cidrs
  database_app_secret_arn      = lookup(module.secret_store.database_secret_arns, "platform_app", "")
  database_migrator_secret_arn = lookup(module.secret_store.database_secret_arns, "platform_migrator", "")
  secret_arns                  = module.secret_store.secret_arns
  secret_kms_key_arn           = coalesce(module.secret_store.kms_key_arn, "")

  temporal = local.temporal_deployed ? {
    endpoint                 = module.temporal_control_plane.endpoint
    namespace                = module.temporal_control_plane.namespace
    server_name              = module.temporal_control_plane.frontend_server_name
    ca_secret_arn            = module.temporal_control_plane.client_ca_secret_arn
    ca_kms_key_arn           = module.temporal_control_plane.tls_kms_key_arn
    client_security_group_id = module.temporal_control_plane.client_security_group_id
  } : null

  langfuse = local.container_langfuse ? {
    web_image        = var.container_langfuse_web_image
    worker_image     = var.container_langfuse_worker_image
    clickhouse_image = var.container_clickhouse_image
    public_url       = var.container_langfuse_public_url != "" ? var.container_langfuse_public_url : "https://langfuse.${var.container_internal_domain}:8444"
    admin_email      = var.container_langfuse_admin_email
    cache = {
      endpoint          = module.elasticache.primary_endpoint_address
      port              = module.elasticache.port
      username          = module.elasticache.username
      security_group_id = module.elasticache.security_group_id
    }
    clickhouse_storage = {
      file_system_id    = module.clickhouse_storage.file_system_id
      file_system_arn   = module.clickhouse_storage.file_system_arn
      access_point_id   = module.clickhouse_storage.access_point_id
      access_point_arn  = module.clickhouse_storage.access_point_arn
      security_group_id = module.clickhouse_storage.security_group_id
    }
    bucket = {
      name        = module.langfuse_blob_store.bucket_id
      arn         = module.langfuse_blob_store.bucket_arn
      kms_key_arn = var.kms_key_arn
    }
  } : null
  langfuse_cache_secret_arn            = local.container_langfuse ? module.elasticache.user_secret_arn : ""
  tls_sidecar_image                    = var.container_tls_sidecar_image
  collector_image                      = var.container_collector_image
  collector_exports_enabled            = var.container_collector_exports_enabled
  xray_encryption_enabled              = var.container_xray_encryption_enabled
  langfuse_task_sizes                  = var.container_langfuse_task_sizes
  internal_operator_security_group_ids = [module.bedrock_jumphost.task_security_group_id]
  workspace_client_security_group_ids  = local.eks_enabled ? [module.eks_cluster.cluster_security_group_id] : []
  secret_recovery_window_days          = var.secret_recovery_window_days

  bedrock_invoke_policy_arns = [module.bedrock_iam.invoke_policy_arn]
  enable_execute_command     = var.container_enable_execute_command
  exec_task_policy_arn       = module.ecs_cluster.exec_task_policy_arn

  tags = var.tags
}
