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
  kms_key_arn             = var.kms_key_arn
  log_kms_key_arn         = var.kms_key_arn
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
