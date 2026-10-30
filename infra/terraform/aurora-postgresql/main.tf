data "aws_subnet" "selected" {
  count = var.create_cluster ? length(var.subnet_ids) : 0

  id = var.subnet_ids[count.index]
}

locals {
  enabled            = var.create_cluster
  cluster_identifier = "${var.name_prefix}-aurora"
  engine             = "aurora-postgresql"
  engine_major       = split(".", var.engine_version)[0]
  serverless         = var.instance_class == "db.serverless"
  subnet_azs         = distinct([for subnet in data.aws_subnet.selected : subnet.availability_zone])
  tags               = merge(var.tags, var.bedtime_tags)

  default_cluster_parameters = {
    "rds.force_ssl"            = { value = "1", apply_method = "immediate" }
    "shared_preload_libraries" = { value = "pg_stat_statements,pgaudit", apply_method = "pending-reboot" }
    "pgaudit.log"              = { value = "ddl", apply_method = "immediate" }
    "log_connections"          = { value = "1", apply_method = "immediate" }
    "log_disconnections"       = { value = "1", apply_method = "immediate" }
    "log_lock_waits"           = { value = "1", apply_method = "immediate" }
    "log_statement"            = { value = "ddl", apply_method = "immediate" }
    "log_min_error_statement"  = { value = "error", apply_method = "immediate" }
    "log_temp_files"           = { value = "0", apply_method = "immediate" }
  }
  cluster_parameters = merge(local.default_cluster_parameters, var.cluster_parameters)
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.enabled || (var.vpc_id != "" && length(local.subnet_azs) >= 2)
      error_message = "create_cluster requires vpc_id and subnet_ids spanning at least two Availability Zones."
    }
    precondition {
      condition     = !local.enabled || length(var.ingress_security_group_ids) + length(var.ingress_cidr_blocks) > 0
      error_message = "create_cluster requires ingress_security_group_ids or ingress_cidr_blocks."
    }
    precondition {
      condition     = !local.serverless || var.serverless_max_capacity >= var.serverless_min_capacity
      error_message = "serverless_max_capacity must be at least serverless_min_capacity."
    }
  }
}

resource "aws_db_subnet_group" "this" {
  count = local.enabled ? 1 : 0

  name       = local.cluster_identifier
  subnet_ids = var.subnet_ids
  tags       = merge(var.tags, { Name = local.cluster_identifier })
}

resource "aws_security_group" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.cluster_identifier}-"
  description = "Aurora PostgreSQL. Admits the database port from the application and management tiers. No egress, because the data tier never originates connections."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = local.cluster_identifier })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "from_security_group" {
  count = local.enabled ? length(var.ingress_security_group_ids) : 0

  security_group_id            = aws_security_group.this[0].id
  referenced_security_group_id = var.ingress_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = var.port
  to_port                      = var.port
  description                  = "PostgreSQL from an admitted security group"
}

resource "aws_vpc_security_group_ingress_rule" "from_cidr" {
  for_each = local.enabled ? toset(var.ingress_cidr_blocks) : toset([])

  security_group_id = aws_security_group.this[0].id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = var.port
  to_port           = var.port
  description       = "PostgreSQL from an admitted tier"
}

resource "aws_rds_cluster_parameter_group" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.cluster_identifier}-"
  family      = "${local.engine}${local.engine_major}"
  description = "Platform defaults for ${local.cluster_identifier}. TLS enforced, pgaudit DDL logging, connection logging."

  dynamic "parameter" {
    for_each = local.cluster_parameters
    content {
      name         = parameter.key
      value        = parameter.value.value
      apply_method = parameter.value.apply_method
    }
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "postgresql" {
  count = local.enabled ? 1 : 0

  name              = "/aws/rds/cluster/${local.cluster_identifier}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn == "" ? null : var.log_kms_key_arn
  tags              = var.tags
}

resource "random_id" "final_snapshot" {
  count = local.enabled ? 1 : 0

  byte_length = 4
}

resource "aws_rds_cluster" "this" {
  count = local.enabled ? 1 : 0

  cluster_identifier = local.cluster_identifier
  engine             = local.engine
  engine_mode        = "provisioned"
  engine_version     = var.engine_version
  database_name      = var.database_name
  port               = var.port

  master_username               = var.master_username
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.master_user_secret_kms_key_arn == "" ? null : var.master_user_secret_kms_key_arn

  iam_database_authentication_enabled = true
  enable_http_endpoint                = var.enable_data_api
  storage_encrypted                   = true
  kms_key_id                          = var.kms_key_arn == "" ? null : var.kms_key_arn

  db_subnet_group_name            = aws_db_subnet_group.this[0].name
  vpc_security_group_ids          = [aws_security_group.this[0].id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this[0].name

  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = var.preferred_backup_window
  preferred_maintenance_window = var.preferred_maintenance_window
  copy_tags_to_snapshot        = true
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = var.skip_final_snapshot
  final_snapshot_identifier    = "${local.cluster_identifier}-final-${random_id.final_snapshot[0].hex}"
  allow_major_version_upgrade  = false

  enabled_cloudwatch_logs_exports = ["postgresql"]

  dynamic "serverlessv2_scaling_configuration" {
    for_each = local.serverless ? [1] : []
    content {
      min_capacity             = var.serverless_min_capacity
      max_capacity             = var.serverless_max_capacity
      seconds_until_auto_pause = var.serverless_min_capacity == 0 ? var.seconds_until_auto_pause : null
    }
  }

  tags = merge(local.tags, { Name = local.cluster_identifier })

  depends_on = [aws_cloudwatch_log_group.postgresql]

  lifecycle {
    ignore_changes = [engine_version]
  }
}

resource "aws_rds_cluster_instance" "this" {
  count = local.enabled ? var.instance_count : 0

  identifier                      = "${local.cluster_identifier}-${count.index}"
  cluster_identifier              = aws_rds_cluster.this[0].id
  instance_class                  = var.instance_class
  engine                          = aws_rds_cluster.this[0].engine
  engine_version                  = aws_rds_cluster.this[0].engine_version
  db_subnet_group_name            = aws_db_subnet_group.this[0].name
  publicly_accessible             = false
  ca_cert_identifier              = var.ca_cert_identifier
  auto_minor_version_upgrade      = var.auto_minor_version_upgrade
  performance_insights_enabled    = var.performance_insights_enabled
  performance_insights_kms_key_id = var.performance_insights_enabled && var.kms_key_arn != "" ? var.kms_key_arn : null
  copy_tags_to_snapshot           = true

  tags = merge(local.tags, { Name = "${local.cluster_identifier}-${count.index}" })

  lifecycle {
    ignore_changes = [engine_version]
  }
}
