mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_elb_service_account" {
    defaults = { arn = "arn:aws:iam::127311923021:root" }
  }
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = { id = "pl-63a5400a" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/agentic-platform-internal/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:listener/app/agentic-platform-internal/1/2" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/agentic-platform/3" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/agentic-platform-role" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111111111111:key/services" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-platform/x" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/platform" }
  }
  mock_resource "aws_efs_file_system" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:111111111111:file-system/fs-1", id = "fs-1" }
  }
  mock_resource "aws_efs_access_point" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:111111111111:access-point/fsap-1", id = "fsap-1" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:111111111111:task-definition/agentic-platform-x:1" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/clickhouse/tls-g" }
  }
  mock_resource "aws_prometheus_workspace" {
    defaults = {
      arn                 = "arn:aws:aps:us-east-1:111111111111:workspace/ws-telemetry"
      id                  = "ws-telemetry"
      prometheus_endpoint = "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-telemetry/"
    }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:us-east-1:111111111111:service/srv-1" }
  }
}

override_resource {
  target = aws_iam_role.workload_execution["clickhouse"]
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-clickhouse-exec" }
}

override_resource {
  target = aws_iam_role.workload_execution["langfuse-web"]
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-lf-web-exec" }
}

override_resource {
  target = aws_iam_role.workload_execution["langfuse-worker"]
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-lf-worker-exec" }
}

override_resource {
  target = aws_iam_role.workload_execution["otel-collector"]
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-otel-exec" }
}

override_resource {
  target = aws_security_group.workload["clickhouse"]
  values = { id = "sg-0clickhouse" }
}

override_resource {
  target = aws_security_group.internal_client
  values = { id = "sg-0internalclient" }
}

variables {
  name_prefix                  = "agentic-platform"
  create_container_services    = true
  vpc_id                       = "vpc-1"
  application_subnet_ids       = ["subnet-app-a", "subnet-app-b"]
  management_subnet_ids        = ["subnet-mgmt-a"]
  data_subnet_ids              = ["subnet-data-a", "subnet-data-b"]
  endpoints_security_group_id  = "sg-0endpoints"
  access_log_bucket            = "agentic-platform-access-logs-111111111111"
  internal_acm_certificate_arn = "arn:aws:acm:us-east-1:111111111111:certificate/11111111-2222-3333-4444-555555555555"
  cluster_arn                  = "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
  platform_image               = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/platform:0.3.0.1-abc-arm64@sha256:1"
  repo_intelligence_image      = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/repo-intelligence:0.3.0-def-arm64@sha256:2"
  database_host                = "agentic-platform.cluster-abc.us-east-1.rds.amazonaws.com"
  database_cidr_blocks         = ["10.0.11.0/24", "10.0.12.0/24"]
  database_app_secret_arn      = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/platform_app-a"
  database_migrator_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/platform_migrator-b"
  secret_kms_key_arn           = "arn:aws:kms:us-east-1:111111111111:key/secrets"
  bedrock_invoke_policy_arn    = "arn:aws:iam::111111111111:policy/agentic-platform-bedrock-invoke"
  secret_arns = {
    coordination_shared_secret = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/coordination-c"
    review_signing_key         = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/external/review-signing-key-d"
    review_attestation_secret  = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/review-attestation-e"
    database_langfuse          = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/langfuse-h"
    clickhouse_password        = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/clickhouse-password-i"
    langfuse_salt              = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-salt-j"
    langfuse_encryption_key    = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-encryption-key-k"
    langfuse_nextauth_secret   = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-nextauth-secret-l"
    langfuse_public_key        = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-public-key-m"
    langfuse_secret_key        = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-secret-key-n"
    langfuse_admin_password    = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/langfuse-admin-password-o"
  }
  collector_image           = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/otel-collector:0.160.0-abc-arm64@sha256:7"
  use_fips_endpoints        = true
  tls_sidecar_image         = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/tls-sidecar:0.3.0-abc-arm64@sha256:6"
  langfuse_cache_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/elasticache/langfuse-p"
  langfuse = {
    web_image        = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/langfuse-web:4.59.0@sha256:3"
    worker_image     = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/langfuse-worker:4.59.0@sha256:4"
    clickhouse_image = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/clickhouse:26.9.19.9@sha256:5"
    public_url       = "https://langfuse.platform.internal:8444"
    admin_email      = "operator@example.com"
    cache = {
      endpoint          = "master.agentic-platform-cache.abc.use1.cache.amazonaws.com"
      port              = 6379
      username          = "langfuse"
      security_group_id = "sg-0cache"
    }
    clickhouse_storage = {
      file_system_id    = "fs-clickhouse"
      file_system_arn   = "arn:aws:elasticfilesystem:us-east-1:111111111111:file-system/fs-clickhouse"
      access_point_id   = "fsap-clickhouse"
      access_point_arn  = "arn:aws:elasticfilesystem:us-east-1:111111111111:access-point/fsap-clickhouse"
      security_group_id = "sg-0clickhousestorage"
    }
    bucket = {
      name        = "agentic-platform-langfuse-111111111111"
      arn         = "arn:aws:s3:::agentic-platform-langfuse-111111111111"
      kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/langfuse"
    }
  }
  temporal = {
    endpoint                 = "agentic-platform-temporal-1.elb.us-east-1.amazonaws.com:7233"
    namespace                = "default"
    server_name              = "frontend.temporal.internal"
    ca_secret_arn            = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/temporal/tls-ca-f"
    ca_kms_key_arn           = "arn:aws:kms:us-east-1:111111111111:key/temporal"
    client_security_group_id = "sg-0temporalclient"
  }
  environment_config = { "tickets.yaml" = "backend: rebar\n" }
  platform_ca_pem    = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
}

run "without_a_collector_image_the_review_worker_exports_to_langfuse_itself" {
  command = plan

  variables {
    collector_image = ""
  }

  assert {
    condition = (
      length(aws_ecs_service.collector) == 0 && output.telemetry == null &&
      contains([for variable in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].environment : variable.name], "LANGFUSE_HOST") &&
      contains([for variable in jsondecode(aws_ecs_task_definition.workload["workflow-worker"].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "OTEL_TRACES_EXPORTER=none")
    )
    error_message = "Without a collector, a task exports to one, or the review worker does not export to Langfuse."
  }
}

run "the_collector_receives_every_platform_task_behind_the_load_balancer" {
  command = apply

  assert {
    condition = (
      one(aws_ecs_service.collector["otel-collector"].load_balancer).container_port == 4317 &&
      alltrue([
        for name in ["workflow-worker", "review-worker", "coordination", "schedule-bootstrap"] :
        contains([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.platform.internal:443") &&
        !contains([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : variable.name], "OTEL_TRACES_EXPORTER") &&
        contains(local.workload_security_groups[name], "sg-0internalclient")
      ])
    )
    error_message = "A platform task does not export to the collector through the internal load balancer."
  }

  assert {
    condition = (
      !contains([for variable in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].environment : variable.name], "LANGFUSE_HOST") &&
      contains(output.secret_readers.langfuse_secret_key, aws_iam_role.workload_execution["review-worker"].arn) &&
      toset([for secret in jsondecode(aws_ecs_task_definition.collector["otel-collector"].container_definitions)[1].secrets : secret.name]) == toset(["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
    )
    error_message = "The review worker still exports to Langfuse itself, which would record each generation twice."
  }
}

run "the_collector_exports_to_xray_prometheus_and_langfuse_only" {
  command = apply

  assert {
    condition = (
      strcontains(local.collector_config, "endpoint: https://xray-fips.us-east-1.amazonaws.com") &&
      strcontains(local.collector_config, "endpoint: https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-telemetry/api/v1/remote_write") &&
      strcontains(local.collector_config, "exporters: [otlphttp/langfuse]") &&
      strcontains(local.collector_config, "processors: [memory_limiter, attributes/redact, filter/genai, batch]")
    )
    error_message = "The collector does not export traces to X-Ray over FIPS, metrics to the workspace, and generations to Langfuse."
  }

  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.workload_task["otel-collector"].policy).Statement : statement.Action])) ==
      toset(["xray:PutTraceSegments", "xray:PutTelemetryRecords", "aps:RemoteWrite"]) &&
      [for statement in jsondecode(aws_iam_role_policy.workload_task["otel-collector"].policy).Statement : statement.Resource if contains(statement.Action, "aps:RemoteWrite")] == ["arn:aws:aps:us-east-1:111111111111:workspace/ws-telemetry"]
    )
    error_message = "The collector task role holds an action beyond X-Ray writes and remote write to its workspace."
  }

  assert {
    condition     = length(aws_xray_encryption_config.telemetry) == 0
    error_message = "The module changed the region's X-Ray encryption without xray_encryption_enabled."
  }
}

run "with_exports_off_the_collector_sends_nothing_outside_the_task" {
  command = apply

  variables {
    collector_exports_enabled = false
  }

  assert {
    condition = (
      length(aws_ecs_service.collector) == 1 && length(aws_prometheus_workspace.telemetry) == 0 &&
      !strcontains(local.collector_config, "awsxray") && !strcontains(local.collector_config, "prometheusremotewrite") &&
      !strcontains(local.collector_config, "otlphttp") && !contains(keys(aws_iam_role_policy.workload_task), "otel-collector")
    )
    error_message = "With exports off, the collector still has an exporter or a permission that reaches outside its task."
  }
}

run "xray_encryption_uses_the_telemetry_key_on_request" {
  command = apply

  variables {
    xray_encryption_enabled = true
  }

  assert {
    condition = (
      aws_xray_encryption_config.telemetry[0].key_id == aws_kms_key.telemetry[0].arn &&
      contains(flatten([for statement in jsondecode(aws_iam_role_policy.workload_task["otel-collector"].policy).Statement : statement.Action]), "kms:GenerateDataKey")
    )
    error_message = "X-Ray encryption does not use the telemetry key, or the collector cannot encrypt its segments."
  }
}

run "a_collector_without_the_sidecar_image_is_refused" {
  command = plan

  variables {
    tls_sidecar_image = ""
    langfuse          = null
  }

  expect_failures = [terraform_data.collector_guard]
}

run "each_service_restarts_after_a_rotation_of_a_credential_it_reads" {
  command = apply

  assert {
    condition = (
      toset(output.secret_restart_services.database_platform_app) == toset([
        for name in ["workflow-worker-blue", "workflow-worker-green", "review-worker-blue", "review-worker-green", "coordination"] : aws_ecs_service.platform[name].id
      ]) &&
      output.secret_restart_services.coordination_shared_secret == [aws_ecs_service.platform["coordination"].id] &&
      alltrue([for services in values(output.secret_restart_services) : length(services) <= 5])
    )
    error_message = "A rotated credential would not restart every service that reads it, or a secret lists more than the five services the secret store allows."
  }
}
