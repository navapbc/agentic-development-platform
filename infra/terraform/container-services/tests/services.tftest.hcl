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

run "no_images_create_no_service" {
  command = plan

  variables {
    platform_image = ""
  }

  assert {
    condition     = length(aws_ecs_service.platform) == 0 && length(aws_ecs_task_definition.workload) == 0 && output.deploy == null
    error_message = "A service was planned without its image."
  }
}

run "every_task_injects_secrets_by_arn_and_holds_none" {
  command = apply

  assert {
    condition = alltrue([
      for name, definition in aws_ecs_task_definition.workload : alltrue([
        for container in jsondecode(definition.container_definitions) :
        container.readonlyRootFilesystem && alltrue([
          for variable in try(container.environment, []) :
          !can(regex("(?i)password|secret_key|private key|signing_key", variable.name)) && !can(regex("PRIVATE KEY", variable.value))
        ])
      ])
    ])
    error_message = "A task definition holds a secret value or a writable root filesystem."
  }

  assert {
    condition = alltrue([
      for name in ["workflow-worker", "review-worker", "coordination", "db-migrate", "schedule-bootstrap"] :
      contains([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : "${variable.name}=${variable.value}"], "PGSSLMODE=verify-full") &&
      !strcontains(one([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : variable.value if variable.name == "DATABASE_URL"]), "@")
    ])
    error_message = "A database client does not verify the server certificate, or carries credentials in DATABASE_URL."
  }

  assert {
    condition = (
      [for secret in jsondecode(aws_ecs_task_definition.workload["db-migrate"].container_definitions)[1].secrets : secret.valueFrom if secret.name == "PGPASSWORD"] ==
      ["${var.database_migrator_secret_arn}:password::"] &&
      [for secret in jsondecode(aws_ecs_task_definition.workload["coordination"].container_definitions)[1].secrets : secret.valueFrom if secret.name == "PGPASSWORD"] ==
      ["${var.database_app_secret_arn}:password::"]
    )
    error_message = "db-migrate does not run as the migrator role, or a service runs as it."
  }
}

run "each_execution_role_reads_only_its_secrets" {
  command = apply

  assert {
    condition = (
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_execution["workflow-worker"].policy).Statement : statement.Resource if statement.Sid == "ReadTheInjectedSecrets"])) ==
      toset([var.database_app_secret_arn, var.temporal.ca_secret_arn]) &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_execution["review-worker"].policy).Statement : statement.Resource if statement.Sid == "ReadTheInjectedSecrets"])) ==
      toset([var.database_app_secret_arn, var.temporal.ca_secret_arn, var.secret_arns.review_signing_key, var.secret_arns.review_attestation_secret]) &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_execution["db-migrate"].policy).Statement : statement.Resource if statement.Sid == "ReadTheInjectedSecrets"])) ==
      toset([var.database_migrator_secret_arn])
    )
    error_message = "An execution role reads a secret its task does not inject."
  }

  assert {
    condition     = alltrue([for statement in jsondecode(aws_iam_role_policy.workload_execution["repo-intelligence"].policy).Statement : statement.Sid != "ReadTheInjectedSecrets"])
    error_message = "repo-intelligence reads a secret."
  }
}

run "only_the_review_worker_calls_bedrock_and_only_the_index_writers_write_the_index" {
  command = apply

  assert {
    condition     = length(aws_iam_role_policy_attachment.review_bedrock) == 1 && aws_iam_role_policy_attachment.review_bedrock[0].policy_arn == var.bedrock_invoke_policy_arn
    error_message = "The review worker task role lacks the Bedrock policy."
  }

  assert {
    condition     = toset(keys(aws_iam_role_policy.workload_task)) == toset(["review-worker", "repo-intelligence", "repo-index-worker"])
    error_message = "A task role grants AWS actions to a task that calls none."
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_role_policy.workload_task["review-worker"].policy).Statement : statement.Action if statement.Sid == "MountCheckouts"]) == ["elasticfilesystem:ClientMount"] &&
      one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-index-worker"].policy).Statement : statement.Action if statement.Sid == "MountIndex"]) == ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    )
    error_message = "The review worker can write the checkouts, or the index worker cannot write the index."
  }
}

run "each_service_joins_only_the_groups_it_needs" {
  command = apply

  assert {
    condition = (
      contains(aws_ecs_service.platform["workflow-worker-blue"].network_configuration[0].security_groups, "sg-0temporalclient") &&
      !contains(aws_ecs_service.platform["workflow-worker-blue"].network_configuration[0].security_groups, "sg-0internalclient") &&
      contains(aws_ecs_service.platform["review-worker-green"].network_configuration[0].security_groups, "sg-0internalclient") &&
      !contains(aws_ecs_service.platform["repo-intelligence"].network_configuration[0].security_groups, "sg-0temporalclient")
    )
    error_message = "A service joins a group it does not call through, or misses one it does."
  }

  assert {
    condition     = length([for key, rule in aws_vpc_security_group_egress_rule.workload_to_database : key if startswith(key, "repo-")]) == 0
    error_message = "A repository-intelligence task can reach the database."
  }

  assert {
    condition = (
      [for balancer in aws_ecs_service.platform["coordination"].load_balancer : balancer.container_port] == [8130] &&
      aws_ecs_service.platform["coordination"].desired_count == 0
    )
    error_message = "coordination does not register on its target port, or starts before inv platform-deploy."
  }
}

run "each_temporal_worker_has_a_blue_and_a_green_slot" {
  command = apply

  assert {
    condition = toset(keys(aws_ecs_service.platform)) == toset([
      "workflow-worker-blue", "workflow-worker-green", "review-worker-blue", "review-worker-green",
      "coordination", "repo-intelligence", "repo-index-worker",
    ])
    error_message = "A Temporal worker lacks a slot, or another service has slots."
  }

  assert {
    condition = (
      output.deploy.versioned_services["workflow-worker"].slots == { blue = "agentic-platform-workflow-worker-blue", green = "agentic-platform-workflow-worker-green" } &&
      aws_ecs_service.platform["workflow-worker-green"].desired_count == 0
    )
    error_message = "The deploy output does not name both slots, or a slot starts before inv platform-deploy."
  }
}

run "the_workspace_admits_tls_and_access_points_only" {
  command = apply

  assert {
    condition = (
      jsondecode(aws_efs_file_system_policy.workspace[0].policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      jsondecode(aws_efs_file_system_policy.workspace[0].policy).Statement[1].Effect == "Deny" &&
      aws_efs_file_system.workspace[0].encrypted
    )
    error_message = "The workspace file system admits clients without TLS or outside its access points, or is unencrypted."
  }
}

run "a_service_without_its_inputs_is_refused" {
  command = plan

  variables {
    secret_arns = {}
  }

  expect_failures = [terraform_data.services_guard]
}
