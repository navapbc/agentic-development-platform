data "aws_caller_identity" "current" {}

locals {
  enabled        = var.create_jumphost
  task_label     = "${var.name_prefix}-${var.jumphost_name}"
  log_group_name = "/aws/ecs/fargate/${local.task_label}"
  param_prefix   = var.jumphost_env == "" ? "/jumphost/${var.jumphost_name}" : "/${var.jumphost_env}/jumphost/${var.jumphost_name}"
}

resource "aws_cloudwatch_log_group" "exec" {
  count = local.enabled ? 1 : 0

  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.ecs_exec[0].arn

  tags = merge(var.tags, { Name = "${var.name_prefix}-jumphost-exec-logs" })
}

resource "aws_ecs_cluster" "this" {
  count = local.enabled ? 1 : 0

  name = local.task_label

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  configuration {
    execute_command_configuration {
      kms_key_id = aws_kms_key.ecs_exec[0].arn
      logging    = "OVERRIDE"

      log_configuration {
        cloud_watch_log_group_name     = aws_cloudwatch_log_group.exec[0].name
        cloud_watch_encryption_enabled = true
        s3_bucket_name                 = aws_s3_bucket.exec_logs[0].id
        s3_key_prefix                  = "exec-output"
        s3_bucket_encryption_enabled   = true
      }
    }
  }

  tags = merge(var.tags, { Name = local.task_label })
}

resource "aws_ecs_task_definition" "this" {
  count = local.enabled ? 1 : 0

  family                   = local.task_label
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution[0].arn
  task_role_arn            = aws_iam_role.task[0].arn

  runtime_platform {
    cpu_architecture        = var.cpu_architecture
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = local.task_label
      image     = var.image_uri
      essential = true
      environment = [
        { name = "NAME", value = var.jumphost_name },
        { name = "DURATION_IN_SECS", value = tostring(var.duration_in_secs) },
      ]
      secrets = [
        for s in var.secrets : { name = s.name, valueFrom = s.secret_arn }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = local.log_group_name
          awslogs-region        = var.region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])

  tags = merge(var.tags, { Name = local.task_label })
}
