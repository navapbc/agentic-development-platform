locals {
  # DNS Firewall already resolves *.internal. Any other internal domain is added to its allowlist.
  container_internal_domains = (
    var.create_container_services && var.container_internal_acm_certificate_arn != "" && !endswith(var.container_internal_domain, ".internal")
    ? ["*.${var.container_internal_domain}"] : []
  )
  container_environment_config = var.container_config_dir == "" ? {} : {
    for name in fileset(var.container_config_dir, "*.yaml") : name => file("${var.container_config_dir}/${name}")
  }

  reader_sets = [var.secret_readers, local.temporal_secret_readers, module.container_services.secret_readers]
  secret_readers = {
    for key in distinct(flatten([for readers in local.reader_sets : keys(readers)])) :
    key => distinct(flatten([for readers in local.reader_sets : lookup(readers, key, [])]))
  }
  restart_sets = [var.secret_restart_services, local.temporal_restart_services_by_secret]
  secret_restart_services = {
    for key in distinct(flatten([for services in local.restart_sets : keys(services)])) :
    key => distinct(flatten([for services in local.restart_sets : lookup(services, key, [])]))
  }
}

module "container_services" {
  source = "../../container-services"

  create_container_services = var.create_container_services
  name_prefix               = var.name_prefix

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
  repo_intelligence_repos = var.container_repo_intelligence_repos
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

  bedrock_invoke_policy_arn = module.bedrock_iam.invoke_policy_arn
  enable_execute_command    = var.container_enable_execute_command
  exec_task_policy_arn      = module.ecs_cluster.exec_task_policy_arn

  tags = var.tags
}
