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
  secret_arns = {
    coordination_shared_secret = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/coordination-c"
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
      for name in ["workflow-worker", "coordination", "db-migrate", "schedule-bootstrap"] :
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

run "every_task_reads_the_repository_list_at_the_checkouts_path" {
  command = apply

  assert {
    condition = alltrue([
      for name, definition in aws_ecs_task_definition.workload : alltrue([
        contains([for variable in jsondecode(definition.container_definitions)[1].environment : "${variable.name}=${variable.value}"], "REPOSITORY_ROOT=/repositories"),
        contains([for variable in jsondecode(definition.container_definitions)[1].environment : variable.name], "PLATFORM_ENV_CONFIG"),
        !contains([for variable in jsondecode(definition.container_definitions)[1].environment : variable.name], "ENVIRONMENT"),
      ])
    ])
    error_message = "A task does not find the repositories at the checkouts path, does not receive the platform config, or receives the ENVIRONMENT setting every process refuses."
  }
}

run "each_execution_role_reads_only_its_secrets" {
  command = apply

  assert {
    condition = (
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_execution["workflow-worker"].policy).Statement : statement.Resource if statement.Sid == "ReadTheInjectedSecrets"])) ==
      toset([var.database_app_secret_arn, var.temporal.ca_secret_arn]) &&
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

run "no_review_worker_runs_on_ecs_and_only_the_index_writers_write_the_index" {
  command = apply

  assert {
    condition = (
      !contains(keys(aws_ecs_task_definition.workload), "review-worker") &&
      length([for name in keys(aws_ecs_service.platform) : name if startswith(name, "review-worker")]) == 0 &&
      !contains(keys(aws_iam_role.workload_task), "review-worker") &&
      !contains(keys(aws_iam_role.workload_execution), "review-worker") &&
      !contains(keys(output.deploy.versioned_services), "review-worker") &&
      !contains(keys(output.secret_readers), "review_signing_key") &&
      !contains(keys(output.secret_readers), "review_attestation_secret")
    )
    error_message = "The module plans a review worker, its roles, or its secret readers, and each tenant's review worker runs on EKS."
  }

  assert {
    condition     = toset(keys(aws_iam_role_policy.workload_task)) == toset(["repo-intelligence", "repo-index-worker"])
    error_message = "A task role grants AWS actions to a task that calls none."
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-index-worker"].policy).Statement : statement.Action if statement.Sid == "MountIndex"]) == ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    error_message = "The index worker cannot write the index."
  }
}

run "each_service_joins_only_the_groups_it_needs" {
  command = apply

  assert {
    condition = (
      contains(aws_ecs_service.platform["workflow-worker-blue"].network_configuration[0].security_groups, "sg-0temporalclient") &&
      !contains(aws_ecs_service.platform["workflow-worker-blue"].network_configuration[0].security_groups, "sg-0internalclient") &&
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
      "workflow-worker-blue", "workflow-worker-green",
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

run "the_workspace_admits_listed_client_groups_on_nfs" {
  command = plan

  variables {
    workspace_client_security_group_ids = ["sg-eks-cluster"]
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.workspace_from_clients[0].referenced_security_group_id == "sg-eks-cluster" &&
      aws_vpc_security_group_ingress_rule.workspace_from_clients[0].from_port == 2049 &&
      aws_vpc_security_group_ingress_rule.workspace_from_clients[0].to_port == 2049
    )
    error_message = "A listed client group is not admitted to the workspace mount targets on NFS only."
  }
}

run "a_service_without_its_inputs_is_refused" {
  command = plan

  variables {
    secret_arns = {}
  }

  expect_failures = [terraform_data.services_guard]
}

run "each_tenant_gets_its_own_workspace_root" {
  command = apply

  variables {
    tenant_workspaces = ["adp", "payments"]
  }

  assert {
    condition = (
      aws_efs_access_point.tenant["adp"].root_directory[0].path == "/tenants/adp" &&
      aws_efs_access_point.tenant["payments"].root_directory[0].path == "/tenants/payments" &&
      aws_efs_access_point.tenant["adp"].posix_user[0].uid == 10001 &&
      aws_efs_access_point.tenant["adp"].tags.tenant == "adp"
    )
    error_message = "A tenant access point is not rooted at /tenants/<tenant> as the platform user."
  }

  assert {
    condition     = length(jsondecode(aws_efs_file_system_policy.workspace[0].policy).Statement[1].Condition.StringNotEquals["elasticfilesystem:AccessPointArn"]) == 5
    error_message = "The file system policy does not admit the tenant access points and the tenants root."
  }

  assert {
    condition     = keys(output.workspace.tenant_access_point_arns) == ["adp", "payments"]
    error_message = "The workspace output does not name each tenant's access point."
  }

  assert {
    condition     = aws_efs_access_point.workspace["tenants"].root_directory[0].path == "/tenants"
    error_message = "The tenants access point is not rooted at the path the tenant workers write."
  }

  assert {
    condition     = output.platform_environment.TENANTS == "adp,payments"
    error_message = "The platform environment does not list the tenants, which the workflow worker polls."
  }
}

run "platform_settings_reach_the_platform_environment" {
  command = plan

  variables {
    platform_settings = { TICKET_BACKEND = "rebar", MAX_CONCURRENT_RUNS = "4" }
  }

  assert {
    condition     = output.platform_environment.TICKET_BACKEND == "rebar" && output.platform_environment.MAX_CONCURRENT_RUNS == "4"
    error_message = "The platform settings do not reach the platform environment."
  }
}

run "a_platform_setting_may_not_name_a_secret" {
  command = plan

  variables {
    platform_settings = { GITHUB_TOKEN = "x" }
  }

  expect_failures = [var.platform_settings]
}

run "a_platform_setting_may_not_override_the_module" {
  command = plan

  variables {
    platform_settings = { DATABASE_URL = "postgresql://elsewhere" }
  }

  expect_failures = [terraform_data.platform_settings_guard]
}

run "repository_intelligence_reads_each_tenant_checkouts_and_bundle" {
  command = apply

  variables {
    tenant_workspaces         = ["adp", "payments"]
    tenant_config_bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
    tenant_config_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
    repo_intelligence_tokens = {
      adp      = { secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/repo-intelligence-token-a", kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" }
      payments = { secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/payments/repo-intelligence-token-b", kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/tenant-payments" }
    }
  }

  assert {
    condition = alltrue([
      for name in ["repo-intelligence", "repo-index-worker"] :
      toset([for mount in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].mountPoints : "${mount.sourceVolume}:${mount.containerPath}:${try(mount.readOnly, false)}"]) ==
      toset(["tmp:/tmp:false", "tenants:/tenants:true", "index:/app/repository_indexes:false"])
    ])
    error_message = "Repository intelligence or the index worker does not mount the tenants root read-only in place of the checkouts."
  }

  assert {
    condition = alltrue([
      for name in ["repo-intelligence", "repo-index-worker"] : alltrue([
        for setting in ["TENANTS=adp,payments", "TENANT_CONFIG_BUCKET=agentic-platform-tenant-config-abc", "AWS_REGION=us-east-1", "AWS_USE_FIPS_ENDPOINT=true"] :
        contains([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : "${variable.name}=${variable.value}"], setting)
      ])
    ])
    error_message = "Repository intelligence or the index worker does not receive the tenants, the config bucket, and the region."
  }

  assert {
    condition = (
      jsondecode(one([for variable in jsondecode(aws_ecs_task_definition.workload["repo-intelligence"].container_definitions)[1].environment : variable.value if variable.name == "REPO_INTELLIGENCE_TOKEN_SECRET_ARNS"])) == {
        adp      = var.repo_intelligence_tokens.adp.secret_arn
        payments = var.repo_intelligence_tokens.payments.secret_arn
      } &&
      length(try(jsondecode(aws_ecs_task_definition.workload["repo-intelligence"].container_definitions)[1].secrets, [])) == 0 &&
      alltrue([for variable in jsondecode(aws_ecs_task_definition.workload["repo-index-worker"].container_definitions)[1].environment : !startswith(variable.name, "REPO_INTELLIGENCE_TOKEN")]) &&
      length(try(jsondecode(aws_ecs_task_definition.workload["repo-index-worker"].container_definitions)[1].secrets, [])) == 0
    )
    error_message = "Repository intelligence does not receive each tenant's token secret ARN in place of an injected token, or the index worker receives one."
  }

  assert {
    condition = (
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-intelligence"].policy).Statement : statement.Resource if statement.Sid == "ReadTheTenantTokens"])) ==
      toset([var.repo_intelligence_tokens.adp.secret_arn, var.repo_intelligence_tokens.payments.secret_arn]) &&
      one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-intelligence"].policy).Statement : statement.Action if statement.Sid == "ReadTheTenantTokens"]) == "secretsmanager:GetSecretValue" &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-intelligence"].policy).Statement : statement.Resource if statement.Sid == "DecryptTheTenantTokens"])) ==
      toset([var.repo_intelligence_tokens.adp.kms_key_arn, var.repo_intelligence_tokens.payments.kms_key_arn]) &&
      one([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-intelligence"].policy).Statement : statement if statement.Sid == "DecryptTheTenantTokens"]).Condition.StringEquals["kms:ViaService"] == "secretsmanager.us-east-1.amazonaws.com" &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-index-worker"].policy).Statement : statement.Sid != "ReadTheTenantTokens"])
    )
    error_message = "The repository intelligence task role does not read exactly the tenant tokens and their keys through Secrets Manager, or the index worker reads a token."
  }

  assert {
    condition = alltrue([
      for name in ["repo-intelligence", "repo-index-worker"] : alltrue([
        for statement in jsondecode(aws_iam_role_policy.workload_execution[name].policy).Statement :
        statement.Sid != "ReadTheInjectedSecrets" || length(setintersection(toset(flatten([statement.Resource])), toset([var.repo_intelligence_tokens.adp.secret_arn, var.repo_intelligence_tokens.payments.secret_arn]))) == 0
      ])
    ])
    error_message = "An execution role still injects a tenant token."
  }

  assert {
    condition = alltrue([
      for name in ["repo-intelligence", "repo-index-worker"] : (
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement.Action if statement.Sid == "MountTenants"]) == ["elasticfilesystem:ClientMount"] &&
        length([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "MountCheckouts"]) == 0 &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "ListTheTenantBundles"]).Resource == var.tenant_config_bucket_arn &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "ListTheTenantBundles"]).Action == ["s3:ListBucket", "s3:ListBucketVersions"] &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "ListTheTenantBundles"]).Condition.StringLike["s3:prefix"] == ["tenants/*"] &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "ReadTheTenantBundles"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*" &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "ReadTheTenantBundles"]).Action == ["s3:GetObject", "s3:GetObjectVersion"] &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "DecryptTheTenantBundles"]).Resource == var.tenant_config_kms_key_arn &&
        one([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if statement.Sid == "DecryptTheTenantBundles"]).Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
      )
    ])
    error_message = "Repository intelligence or the index worker does not mount the tenants root read-only, or reads the config bucket beyond tenants/."
  }

  assert {
    condition     = contains(keys(aws_vpc_security_group_ingress_rule.workspace_from_workloads), "repo-intelligence") && contains(keys(aws_vpc_security_group_ingress_rule.workspace_from_workloads), "repo-index-worker")
    error_message = "The workspace mount targets do not admit NFS from repository intelligence and the index worker."
  }

  assert {
    condition     = length([for key in keys(output.secret_readers) : key if startswith(key, "repo_intelligence_token")]) == 0 && length([for key in keys(output.secret_restart_services) : key if startswith(key, "repo_intelligence_token")]) == 0
    error_message = "A tenant token reached the secret store's readers or restart services."
  }

  assert {
    condition     = output.repository_intelligence_roles.execution_role_arn != "" && output.repository_intelligence_roles.token_reader_role_arn == aws_iam_role.workload_task["repo-intelligence"].arn && length(output.repository_intelligence_roles.task_role_arns) == 2
    error_message = "The output does not name the repository intelligence execution role, its token reader task role, and the two bundle reader task roles."
  }
}

run "without_tenants_repository_intelligence_reads_the_checkouts" {
  command = apply

  assert {
    condition = alltrue([
      for name in ["repo-intelligence", "repo-index-worker"] : (
        contains([for mount in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].mountPoints : "${mount.sourceVolume}:${mount.containerPath}:${try(mount.readOnly, false)}"], "checkouts:/repositories:true") &&
        !contains([for variable in jsondecode(aws_ecs_task_definition.workload[name].container_definitions)[1].environment : variable.name], "TENANT_CONFIG_BUCKET") &&
        length([for statement in jsondecode(aws_iam_role_policy.workload_task[name].policy).Statement : statement if can(regex("TenantBundles$", statement.Sid))]) == 0
      )
    ])
    error_message = "Without tenants, repository intelligence or the index worker does not mount the checkouts, or reads a config bucket."
  }

  assert {
    condition = (
      !contains([for variable in jsondecode(aws_ecs_task_definition.workload["repo-intelligence"].container_definitions)[1].environment : variable.name], "REPO_INTELLIGENCE_TOKEN_SECRET_ARNS") &&
      length([for statement in jsondecode(aws_iam_role_policy.workload_task["repo-intelligence"].policy).Statement : statement if can(regex("TenantTokens$", statement.Sid))]) == 0
    )
    error_message = "Without tenants, repository intelligence receives a token secret map or reads a tenant token."
  }
}

run "the_token_tenants_match_the_workspaces" {
  command = plan

  variables {
    tenant_workspaces = ["adp"]
    repo_intelligence_tokens = {
      payments = { secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/payments/repo-intelligence-token-b", kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/tenant-payments" }
    }
  }

  expect_failures = [terraform_data.services_guard]
}
