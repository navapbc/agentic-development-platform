locals {
  collector_enabled  = local.services_enabled && var.collector_image != ""
  collector_exports  = local.collector_enabled && var.collector_exports_enabled
  collector_langfuse = local.collector_exports && local.langfuse_enabled
  collector_port     = local.internal_services.otel-collector.port
  collector_url      = "https://otel-collector.${var.internal_domain}:443"
  xray_endpoint      = "https://${var.use_fips_endpoints ? "xray-fips" : "xray"}.${local.region}.amazonaws.com"

  # The redaction, memory, and batch processors are the ones the local collector runs.
  local_collector_config = yamldecode(file("${path.module}/../../../observability/otel/otel-collector-config.yaml"))

  # The direct Langfuse exporter in observability/telemetry.py keeps the spans with a gen_ai.
  # attribute. OTTL cannot match an attribute key by prefix, so this drops the spans that carry
  # none of the gen_ai attributes the platform and pydantic-ai set.
  genai_drop_condition = join(" and ", [
    for key in ["gen_ai.operation.name", "gen_ai.system", "gen_ai.request.model", "gen_ai.response.model"] : "attributes[\"${key}\"] == nil"
  ])

  collector_config = templatefile("${path.module}/collector-config.yaml.tftpl", {
    exports               = local.collector_exports
    langfuse              = local.collector_langfuse
    region                = local.region
    port                  = local.collector_port
    xray_endpoint         = local.xray_endpoint
    remote_write_endpoint = "${try(aws_prometheus_workspace.telemetry[0].prometheus_endpoint, "")}api/v1/remote_write"
    langfuse_endpoint     = "https://langfuse.${var.internal_domain}/api/public/otel"
    genai_drop_condition  = local.genai_drop_condition
    shared_processors = yamlencode({
      memory_limiter      = local.local_collector_config.processors.memory_limiter
      batch               = local.local_collector_config.processors.batch
      "attributes/redact" = local.local_collector_config.processors["attributes/redact"]
    })
  })

  collector_specs = {
    otel-collector = {
      repositories    = ["otel-collector", "tls-sidecar"]
      image           = var.collector_image
      database        = false
      temporal        = false
      internal_client = local.collector_langfuse
      target          = "otel-collector"
      mounts          = []
      one_shot        = false
      secrets         = local.collector_langfuse ? { LANGFUSE_PUBLIC_KEY = "langfuse_public_key", LANGFUSE_SECRET_KEY = "langfuse_secret_key" } : {}
      setup_secrets   = {}
      environment     = {}
      health          = ["CMD", "/usr/local/bin/healthprobe", "http://127.0.0.1:13133/"]
    }
  }
  collector_workloads = { for name, spec in local.collector_specs : name => spec if local.collector_enabled }

  restartable_services = merge(
    { for name, service in aws_ecs_service.langfuse : name => service.id },
    { for name, service in aws_ecs_service.collector : name => service.id },
  )

  collector_statements = [
    for statement in [
      {
        Sid      = "WriteTraceSegments"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
        Include  = local.collector_exports
      },
      {
        Sid      = "RemoteWriteToTheWorkspace"
        Effect   = "Allow"
        Action   = ["aps:RemoteWrite"]
        Resource = try(aws_prometheus_workspace.telemetry[0].arn, "")
        Include  = local.collector_exports
      },
      {
        Sid      = "EncryptTraceSegments"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = try(aws_kms_key.telemetry[0].arn, "")
        Include  = local.collector_exports && var.xray_encryption_enabled
      },
    ] : { for key, value in statement : key => value if key != "Include" } if statement.Include
  ]
}

resource "terraform_data" "collector_guard" {
  count = local.collector_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.tls_sidecar_image != "" && var.platform_ca_pem != ""
      error_message = "The collector needs tls_sidecar_image, which issues its certificate, and platform_ca_pem."
    }
  }
}

resource "aws_kms_key" "telemetry" {
  count = local.collector_exports ? 1 : 0

  description         = "Encrypts the ${var.name_prefix} Prometheus workspace, and the region's X-Ray traces when xray_encryption_enabled."
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
    ]
  })

  tags = var.tags
}

resource "aws_kms_alias" "telemetry" {
  count = local.collector_exports ? 1 : 0

  name          = "alias/${var.name_prefix}-telemetry"
  target_key_id = aws_kms_key.telemetry[0].key_id
}

resource "aws_prometheus_workspace" "telemetry" {
  count = local.collector_exports ? 1 : 0

  alias       = "${var.name_prefix}-telemetry"
  kms_key_arn = aws_kms_key.telemetry[0].arn

  tags = var.tags
}

# X-Ray encryption is one setting per account and region, so enabling it here changes it for
# every X-Ray user in the region.
resource "aws_xray_encryption_config" "telemetry" {
  count = local.collector_exports && var.xray_encryption_enabled ? 1 : 0

  type   = "KMS"
  key_id = aws_kms_key.telemetry[0].arn
}

resource "aws_ecs_task_definition" "collector" {
  for_each = local.collector_workloads

  skip_destroy = true

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.workload_execution[each.key].arn
  task_role_arn            = aws_iam_role.workload_task[each.key].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "collector-tls"
  }

  container_definitions = jsonencode([
    {
      name                   = "issue-certificate"
      image                  = var.tls_sidecar_image
      essential              = false
      user                   = "0"
      readonlyRootFilesystem = true
      command                = ["issue", "-out", "/collector-tls", "-owner", "10001:10001"]
      mountPoints            = [{ sourceVolume = "collector-tls", containerPath = "/collector-tls" }]
      logConfiguration       = local.log_settings[each.key]
    },
    {
      name                   = each.key
      dependsOn              = [{ containerName = "issue-certificate", condition = "SUCCESS" }]
      image                  = each.value.image
      essential              = true
      readonlyRootFilesystem = true
      command                = ["--config=env:COLLECTOR_CONFIG"]
      environment = [
        { name = "COLLECTOR_CONFIG", value = local.collector_config },
        { name = "PLATFORM_CA_PEM", value = var.platform_ca_pem },
        { name = "AWS_REGION", value = local.region },
      ]
      secrets          = local.workload_secrets[each.key]
      mountPoints      = [{ sourceVolume = "collector-tls", containerPath = "/collector-tls", readOnly = true }]
      portMappings     = [{ containerPort = local.collector_port, protocol = "tcp" }]
      logConfiguration = local.log_settings[each.key]
      healthCheck = {
        command     = each.value.health
        interval    = 30
        timeout     = 10
        retries     = 3
        startPeriod = 60
      }
    },
  ])

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.workload]
}

resource "aws_ecs_service" "collector" {
  for_each = local.collector_workloads

  name                              = "${var.name_prefix}-${each.key}"
  cluster                           = var.cluster_arn
  task_definition                   = aws_ecs_task_definition.collector[each.key].arn
  desired_count                     = 0
  enable_execute_command            = var.enable_execute_command
  health_check_grace_period_seconds = 60
  propagate_tags                    = "SERVICE"

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets         = var.application_subnet_ids
    security_groups = local.workload_security_groups[each.key]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.internal["otel-collector"].arn
    container_name   = each.key
    container_port   = local.collector_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener_rule.internal]
}
