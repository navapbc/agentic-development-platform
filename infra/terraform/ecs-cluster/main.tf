data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  enabled      = var.create_ecs_cluster
  cluster_name = var.cluster_name != "" ? var.cluster_name : "${var.name_prefix}-ecs"
  account_id   = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  region       = data.aws_region.current.region

  cluster_arn = "arn:${local.partition}:ecs:${local.region}:${local.account_id}:cluster/${local.cluster_name}"

  exec_log_group_name     = "/aws/ecs/${local.cluster_name}/exec"
  insights_log_group_name = "/aws/ecs/containerinsights/${local.cluster_name}/performance"
}

resource "aws_cloudwatch_log_group" "exec" {
  count = local.enabled ? 1 : 0

  name              = local.exec_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs[0].arn

  tags = merge(var.tags, { Name = "${local.cluster_name}-exec" })
}

# ECS writes Container Insights performance events to this fixed name and creates the group
# unencrypted on first use, so the module creates it first under the logs key.
resource "aws_cloudwatch_log_group" "insights" {
  count = local.enabled && var.container_insights != "disabled" ? 1 : 0

  name              = local.insights_log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs[0].arn

  tags = merge(var.tags, { Name = "${local.cluster_name}-container-insights" })
}

resource "aws_ecs_cluster" "this" {
  count = local.enabled ? 1 : 0

  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = var.container_insights
  }

  configuration {
    execute_command_configuration {
      kms_key_id = aws_kms_key.logs[0].arn
      logging    = "OVERRIDE"

      log_configuration {
        cloud_watch_log_group_name     = aws_cloudwatch_log_group.exec[0].name
        cloud_watch_encryption_enabled = true
      }
    }

    managed_storage_configuration {
      fargate_ephemeral_storage_kms_key_id = aws_kms_key.storage[0].arn
      kms_key_id                           = aws_kms_key.storage[0].arn
    }
  }

  tags = merge(var.tags, { Name = local.cluster_name })

  depends_on = [aws_cloudwatch_log_group.insights]
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  count = local.enabled ? 1 : 0

  cluster_name       = aws_ecs_cluster.this[0].name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = var.default_capacity_provider
    weight            = 1
  }
}
