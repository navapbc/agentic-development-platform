locals {
  enabled              = var.create_cache
  replication_group_id = "${var.name_prefix}-cache"
  user_id              = "${var.name_prefix}-${var.username}"
  engine_major         = split(".", var.engine_version)[0]
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.enabled || (var.vpc_id != "" && length(var.subnet_ids) > 0)
      error_message = "create_cache requires vpc_id and subnet_ids."
    }
    precondition {
      condition     = !local.enabled || length(var.ingress_security_group_ids) + length(var.ingress_cidr_blocks) > 0
      error_message = "create_cache requires ingress_security_group_ids or ingress_cidr_blocks."
    }
    precondition {
      condition     = !var.automatic_failover_enabled || var.num_cache_clusters >= 2
      error_message = "automatic_failover_enabled requires num_cache_clusters of 2 or more."
    }
    precondition {
      condition     = !var.multi_az_enabled || var.automatic_failover_enabled
      error_message = "multi_az_enabled requires automatic_failover_enabled."
    }
    precondition {
      condition     = length(local.replication_group_id) <= 40
      error_message = "name_prefix is too long. The replication group id must be 40 characters or fewer."
    }
  }
}

resource "aws_elasticache_subnet_group" "this" {
  count = local.enabled ? 1 : 0

  name       = local.replication_group_id
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.replication_group_id}-"
  description = "Valkey cache. Admits the cache port from the application tier. No egress, because the data tier never originates connections."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = local.replication_group_id })

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
  description                  = "Valkey from an admitted security group"
}

resource "aws_vpc_security_group_ingress_rule" "from_cidr" {
  for_each = local.enabled ? toset(var.ingress_cidr_blocks) : toset([])

  security_group_id = aws_security_group.this[0].id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = var.port
  to_port           = var.port
  description       = "Valkey from an admitted tier"
}

resource "aws_elasticache_parameter_group" "this" {
  count = local.enabled ? 1 : 0

  name        = "${local.replication_group_id}-valkey${local.engine_major}"
  family      = "valkey${local.engine_major}"
  description = "Platform defaults for ${local.replication_group_id}."

  parameter {
    name  = "maxmemory-policy"
    value = var.maxmemory_policy
  }

  tags = var.tags
}

resource "random_password" "user" {
  count = local.enabled ? 1 : 0

  length  = 40
  special = false
}

resource "aws_elasticache_user" "client" {
  count = local.enabled ? 1 : 0

  user_id       = local.user_id
  user_name     = var.username
  engine        = "valkey"
  access_string = var.access_string

  authentication_mode {
    type      = "password"
    passwords = [random_password.user[0].result]
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [authentication_mode]
  }
}

resource "aws_elasticache_user_group" "this" {
  count = local.enabled ? 1 : 0

  user_group_id = local.replication_group_id
  engine        = "valkey"
  user_ids      = [aws_elasticache_user.client[0].user_id]
  tags          = var.tags
}

resource "aws_secretsmanager_secret" "user" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}/elasticache/${var.username}-"
  description = "Credential of the ${var.username} RBAC user on ${local.replication_group_id}, in the format the ElastiCache rotation function expects."
  kms_key_id  = var.secret_kms_key_arn == "" ? null : var.secret_kms_key_arn
  tags        = var.tags
}

resource "aws_secretsmanager_secret_version" "user" {
  count = local.enabled ? 1 : 0

  secret_id = aws_secretsmanager_secret.user[0].id
  secret_string = jsonencode({
    username = var.username
    password = random_password.user[0].result
    user_arn = aws_elasticache_user.client[0].arn
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_cloudwatch_log_group" "engine" {
  count = local.enabled ? 1 : 0

  name              = "/aws/elasticache/${local.replication_group_id}/engine"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn == "" ? null : var.log_kms_key_arn
  tags              = var.tags
}

resource "aws_elasticache_replication_group" "this" {
  count = local.enabled ? 1 : 0

  replication_group_id = local.replication_group_id
  description          = "Platform cache for the Langfuse queue and cache"
  engine               = "valkey"
  engine_version       = var.engine_version
  node_type            = var.node_type
  port                 = var.port
  parameter_group_name = aws_elasticache_parameter_group.this[0].name

  num_cache_clusters         = var.num_cache_clusters
  automatic_failover_enabled = var.automatic_failover_enabled
  multi_az_enabled           = var.multi_az_enabled

  subnet_group_name  = aws_elasticache_subnet_group.this[0].name
  security_group_ids = [aws_security_group.this[0].id]
  user_group_ids     = [aws_elasticache_user_group.this[0].user_group_id]

  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn == "" ? null : var.kms_key_arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"

  snapshot_retention_limit   = var.snapshot_retention_days
  snapshot_window            = var.snapshot_window
  maintenance_window         = var.maintenance_window
  auto_minor_version_upgrade = true

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.engine[0].name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  tags = merge(var.tags, { Name = local.replication_group_id })
}
