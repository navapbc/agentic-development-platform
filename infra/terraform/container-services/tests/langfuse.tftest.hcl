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

run "without_langfuse_nothing_of_it_is_created" {
  command = plan

  variables {
    langfuse = null
  }

  assert {
    condition = (
      length(aws_ecs_service.langfuse) == 0 && length(aws_ecs_task_definition.langfuse) == 0 &&
      length(aws_service_discovery_private_dns_namespace.data) == 0 && output.clickhouse == null && output.langfuse == null
    )
    error_message = "A Langfuse or ClickHouse resource was planned without the langfuse input."
  }
}

run "langfuse_and_clickhouse_inject_secrets_by_arn_and_field" {
  command = apply

  assert {
    condition = alltrue([
      for name, definition in aws_ecs_task_definition.langfuse : alltrue([
        for container in jsondecode(definition.container_definitions) : container.readonlyRootFilesystem &&
        alltrue([for secret in lookup(container, "secrets", []) : startswith(secret.valueFrom, "arn:aws:secretsmanager:")]) &&
        alltrue([for variable in lookup(container, "environment", []) : !contains(["DATABASE_PASSWORD", "REDIS_AUTH", "CLICKHOUSE_PASSWORD", "SALT", "ENCRYPTION_KEY", "NEXTAUTH_SECRET", "CLICKHOUSE_TLS_KEY"], variable.name)])
      ])
    ])
    error_message = "A Langfuse or ClickHouse container holds a secret value or a writable root file system."
  }

  assert {
    condition = (
      contains([for secret in jsondecode(aws_ecs_task_definition.langfuse["langfuse-web"].container_definitions)[1].secrets : secret.valueFrom], "${var.secret_arns.database_langfuse}:password::") &&
      contains([for secret in jsondecode(aws_ecs_task_definition.langfuse["langfuse-web"].container_definitions)[1].secrets : secret.valueFrom], "${var.langfuse_cache_secret_arn}:password::")
    )
    error_message = "Langfuse does not read the database and cache passwords from their JSON fields."
  }

  assert {
    condition = (
      [for secret in jsondecode(aws_ecs_task_definition.langfuse["clickhouse"].container_definitions)[0].secrets : secret.valueFrom] ==
      ["${aws_secretsmanager_secret.clickhouse_tls[0].arn}:certificate::", "${aws_secretsmanager_secret.clickhouse_tls[0].arn}:private_key::"] &&
      jsondecode(aws_secretsmanager_secret_policy.clickhouse_tls[0].policy).Statement[0].Principal.AWS == "arn:aws:iam::111111111111:role/agentic-platform-clickhouse-exec"
    )
    error_message = "The ClickHouse key reaches a container other than its setup, or a role other than its execution role."
  }
}

run "clickhouse_runs_alone_on_its_file_system_and_admits_only_langfuse" {
  command = apply

  assert {
    condition = (
      aws_ecs_service.langfuse["clickhouse"].deployment_minimum_healthy_percent == 0 &&
      aws_ecs_service.langfuse["clickhouse"].deployment_maximum_percent == 100 &&
      length(aws_ecs_service.langfuse["clickhouse"].service_registries) == 1
    )
    error_message = "A ClickHouse deployment could run two servers on one data directory, or ClickHouse has no name."
  }

  assert {
    condition = (
      toset([for rule in aws_vpc_security_group_ingress_rule.clickhouse_from_clients : rule.from_port]) == toset([8443, 9440]) &&
      length(aws_vpc_security_group_ingress_rule.clickhouse_from_clients) == 6 &&
      alltrue([for rule in aws_vpc_security_group_ingress_rule.clickhouse_from_clients : rule.security_group_id == "sg-0clickhouse"])
    )
    error_message = "ClickHouse admits a port other than its TLS ports, or a client other than the Langfuse tasks."
  }

  assert {
    condition     = strcontains(local.clickhouse_config, "<http_port remove=\"1\"/>") && strcontains(local.clickhouse_config, "<tcp_port remove=\"1\"/>")
    error_message = "ClickHouse still serves plain HTTP or the plain native protocol."
  }
}

run "langfuse_web_sits_behind_the_load_balancer_through_its_sidecar" {
  command = apply

  assert {
    condition = (
      one(aws_ecs_service.langfuse["langfuse-web"].load_balancer).container_name == "tls-sidecar" &&
      one(aws_ecs_service.langfuse["langfuse-web"].load_balancer).container_port == 3443 &&
      length(aws_ecs_service.langfuse["langfuse-worker"].load_balancer) == 0
    )
    error_message = "Langfuse web is not reached through its TLS sidecar, or the worker sits behind the load balancer."
  }
}

run "the_secret_store_lists_the_langfuse_readers_and_restarts" {
  command = apply

  assert {
    condition = (
      toset(output.secret_readers.database_langfuse) == toset(["arn:aws:iam::111111111111:role/agentic-platform-lf-web-exec", "arn:aws:iam::111111111111:role/agentic-platform-lf-worker-exec"]) &&
      !contains(keys(output.secret_readers), "clickhouse_tls") &&
      length(output.secret_restart_services.database_langfuse) == 2 &&
      length(output.secret_restart_services.elasticache_user) == 2 &&
      length(output.secret_restart_services.clickhouse_password) == 3
    )
    error_message = "The secret store would not restart the Langfuse services that read a rotated credential."
  }
}

run "the_review_worker_exports_to_langfuse_without_the_collector" {
  command = apply

  assert {
    condition = (
      contains([for variable in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "LANGFUSE_HOST=https://langfuse.platform.internal") &&
      contains([for variable in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "OTEL_SDK_DISABLED=false") &&
      contains([for variable in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "OTEL_TRACES_EXPORTER=none") &&
      contains([for secret in jsondecode(aws_ecs_task_definition.workload["review-worker"].container_definitions)[1].secrets : secret.name], "LANGFUSE_SECRET_KEY")
    )
    error_message = "The review worker does not export its generations to Langfuse."
  }

  assert {
    condition     = contains(keys(output.deploy.tasks), "clickhouse-migrate") && toset(keys(output.deploy.langfuse_services)) == toset(["clickhouse", "langfuse-web", "langfuse-worker"])
    error_message = "inv platform-deploy would not run ClickHouse, Langfuse, or the ClickHouse migration."
  }
}

run "langfuse_without_an_administrator_is_refused" {
  command = plan

  variables {
    langfuse = merge(var.langfuse, { admin_email = "" })
  }

  expect_failures = [terraform_data.langfuse_guard]
}
