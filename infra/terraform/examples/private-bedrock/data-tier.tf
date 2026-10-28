locals {
  secret_store_database = var.create_secret_store && var.create_aurora
  secret_store_cache    = var.create_secret_store && var.create_cache
}

module "aurora_postgresql" {
  source = "../../aurora-postgresql"

  create_cluster          = var.create_aurora
  name_prefix             = var.name_prefix
  vpc_id                  = local.vpc_id
  subnet_ids              = local.data_subnet_ids
  ingress_cidr_blocks     = local.data_tier_sources
  instance_count          = var.aurora_instance_count
  serverless_max_capacity = var.aurora_serverless_max_capacity
  deletion_protection     = var.aurora_deletion_protection
  skip_final_snapshot     = var.aurora_skip_final_snapshot
  kms_key_arn             = var.kms_key_arn
  log_kms_key_arn         = var.kms_key_arn
  enable_data_api         = local.secret_store_database
  bedtime_tags            = var.aurora_bedtime_tags
  tags                    = var.tags
}

module "elasticache" {
  source = "../../elasticache"

  create_cache        = var.create_cache
  name_prefix         = var.name_prefix
  vpc_id              = local.vpc_id
  subnet_ids          = local.data_subnet_ids
  ingress_cidr_blocks = local.app_cidrs
  num_cache_clusters  = var.cache_num_nodes
  kms_key_arn         = var.kms_key_arn
  log_kms_key_arn     = var.kms_key_arn
  secret_kms_key_arn  = var.create_secret_store ? module.secret_store.kms_key_arn : ""
  tags                = var.tags
}

module "clickhouse_storage" {
  source = "../../clickhouse-storage"

  create_storage     = var.create_clickhouse_storage
  name_prefix        = var.name_prefix
  vpc_id             = local.vpc_id
  subnet_ids         = local.data_subnet_ids
  client_cidr_blocks = local.app_cidrs
  kms_key_arn        = var.kms_key_arn
  tags               = var.tags
}

module "langfuse_blob_store" {
  source = "../../langfuse-blob-store"

  create_bucket     = var.create_langfuse_bucket
  name_prefix       = var.name_prefix
  kms_key_arn       = var.kms_key_arn
  access_log_bucket = var.access_log_bucket
  tags              = var.tags
}

module "secret_store" {
  source = "../../secret-store"

  create_secret_store = var.create_secret_store
  name_prefix         = var.name_prefix
  region              = var.region
  secret_readers      = var.secret_readers
  restart_services    = var.secret_restart_services

  recovery_window_days = var.secret_recovery_window_days

  create_database_credentials = local.secret_store_database
  database_cluster_arn        = local.secret_store_database ? module.aurora_postgresql.cluster_arn : ""
  database_master_secret_arn  = local.secret_store_database ? module.aurora_postgresql.master_user_secret_arn : ""

  rotate_elasticache_user     = local.secret_store_cache
  elasticache_user_secret_arn = local.secret_store_cache ? module.elasticache.user_secret_arn : ""
  elasticache_user_arn        = local.secret_store_cache ? module.elasticache.user_arn : ""

  vpc_id                      = local.vpc_id
  subnet_ids                  = local.management_subnet_ids
  endpoints_security_group_id = local.endpoints_security_group_id

  tags = var.tags
}
