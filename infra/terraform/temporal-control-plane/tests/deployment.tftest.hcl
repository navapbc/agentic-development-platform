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
  mock_data "aws_elb_service_account" {
    defaults = { arn = "arn:aws:iam::127311923021:root" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_lb" {
    defaults = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/net/platform/1"
      dns_name = "platform-temporal.elb.us-east-1.amazonaws.com"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/platform/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:listener/net/platform/1/1" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111111111111:key/temporal" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/temporal" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-temporal/task" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:111111111111:task-definition/temporal:1" }
  }
  mock_resource "aws_ecs_service" {
    defaults = { id = "arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/temporal" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:111111111111:agentic-platform-temporal-certificate-expiry" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:111111111111:rule/agentic-platform-temporal-certificate-expiry" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/temporal" }
  }
}

override_resource {
  target = aws_iam_role.execution["server"]
  values = { arn = "arn:aws:iam::111111111111:role/temporal-server-exec" }
}

override_resource {
  target = aws_iam_role.execution["admin_tools"]
  values = { arn = "arn:aws:iam::111111111111:role/temporal-admin-exec" }
}

override_resource {
  target = aws_iam_role.execution["ui"]
  values = { arn = "arn:aws:iam::111111111111:role/temporal-ui-exec" }
}

override_resource {
  target = aws_secretsmanager_secret.tls["ca"]
  values = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/temporal/tls-ca" }
}

override_resource {
  target = aws_secretsmanager_secret.tls["internode"]
  values = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/temporal/tls-internode" }
}

override_resource {
  target = aws_secretsmanager_secret.tls["frontend"]
  values = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/temporal/tls-frontend" }
}

variables {
  name_prefix                   = "agentic-platform"
  create_temporal_control_plane = true
  cluster_arn                   = "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
  vpc_id                        = "vpc-1"
  application_subnet_ids        = ["subnet-app-a"]
  management_subnet_ids         = ["subnet-mgmt-a", "subnet-mgmt-b"]
  endpoints_security_group_id   = "sg-endpoints"
  database_host                 = "platform.cluster-abc.us-east-1.rds.amazonaws.com"
  database_cidr_blocks          = ["10.0.11.0/24", "10.0.12.0/24"]
  database_secret_arn           = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/temporal-x"
  database_secret_kms_key_arn   = "arn:aws:kms:us-east-1:111111111111:key/secrets"
  server_image                  = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/temporal-server:1.32.1-a-arm64@sha256:1"
  admin_tools_image             = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/temporal-admin-tools:1.32.1-b-arm64@sha256:2"
  ui_image                      = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/temporal-ui:2.58.0-c-arm64@sha256:3"
  ui_acm_certificate_arn        = "arn:aws:acm:us-east-1:111111111111:certificate/ui"
  ui_allowed_cidr_blocks        = ["10.0.21.0/24", "10.0.22.0/24"]
  access_log_bucket             = "agentic-platform-access-logs-111111111111"
}

run "no_images_deploy_nothing" {
  command = plan

  variables {
    server_image      = ""
    admin_tools_image = ""
  }

  assert {
    condition     = length(aws_ecs_service.server) == 0 && length(aws_lb.frontend) == 0 && length(aws_iam_role.execution) == 0
    error_message = "The module deployed a server without images."
  }
}

run "tasks_carry_no_secret_values_and_a_read_only_root" {
  command = apply

  assert {
    condition = alltrue(flatten([
      for definition in [aws_ecs_task_definition.server[0], aws_ecs_task_definition.admin_tools[0], aws_ecs_task_definition.ui[0]] : [
        for container in jsondecode(definition.container_definitions) : [
          container.readonlyRootFilesystem,
          alltrue([for variable in try(container.environment, []) : !can(regex("(?i)(PWD|PASSWORD|KEY_PEM|CERT_PEM|CA_PEM|PRIVATE KEY)", "${variable.name}=${variable.value}"))]),
        ]
      ]
    ]))
    error_message = "A container carries a secret value in its environment or a writable root filesystem."
  }

  assert {
    condition = toset([for secret in jsondecode(aws_ecs_task_definition.server[0].container_definitions)[1].secrets : secret.name]) == toset([
      "POSTGRES_USER", "POSTGRES_PWD", "TEMPORAL_TLS_CA_PEM", "TEMPORAL_TLS_INTERNODE_CERT_PEM",
      "TEMPORAL_TLS_INTERNODE_KEY_PEM", "TEMPORAL_TLS_FRONTEND_CERT_PEM", "TEMPORAL_TLS_FRONTEND_KEY_PEM",
    ])
    error_message = "The server does not inject exactly the database credentials and the TLS material."
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.server[0].container_definitions)[1].entryPoint == ["/etc/temporal/platform-start.sh"]
    error_message = "The server does not start through the platform start script."
  }

  assert {
    condition     = aws_ecs_service.server[0].desired_count == 0
    error_message = "The server starts before inv temporal-deploy applies the schema."
  }
}

run "only_the_server_reads_the_keys" {
  command = apply

  assert {
    condition     = jsondecode(local.secret_policies["internode"]).Statement[0].Principal.AWS == [aws_iam_role.execution["server"].arn]
    error_message = "A role other than the server execution role may read the internode key."
  }

  assert {
    condition = toset(jsondecode(local.secret_policies["ca"]).Statement[0].Principal.AWS) == toset([
      aws_iam_role.execution["server"].arn, aws_iam_role.execution["admin_tools"].arn, aws_iam_role.execution["ui"].arn,
    ])
    error_message = "The CA secret is not readable by every task."
  }

  assert {
    condition = (
      !contains(local.roles["admin_tools"].secrets, aws_secretsmanager_secret.tls["internode"].arn) &&
      !contains(local.roles["ui"].secrets, aws_secretsmanager_secret.tls["internode"].arn) &&
      !contains(local.roles["ui"].secrets, var.database_secret_arn)
    )
    error_message = "The admin-tools or UI execution role may read a secret it does not inject."
  }
}

run "the_frontend_admits_the_client_group_only" {
  command = apply

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.nlb_from_clients[0].referenced_security_group_id == aws_security_group.client[0].id &&
      aws_vpc_security_group_ingress_rule.nlb_from_clients[0].from_port == 7233 &&
      aws_vpc_security_group_ingress_rule.server_from_nlb[0].referenced_security_group_id == aws_security_group.nlb[0].id
    )
    error_message = "The NLB or the server admits a source other than the client group and the NLB."
  }

  assert {
    condition = alltrue([
      for rule in aws_vpc_security_group_ingress_rule.server_internode : rule.referenced_security_group_id == aws_security_group.server[0].id
    ])
    error_message = "The internode ports admit a source other than the server tasks."
  }

  assert {
    condition     = aws_lb.frontend[0].internal && aws_lb_listener.frontend[0].protocol == "TCP"
    error_message = "The frontend NLB is public, or it terminates TLS without an ACM certificate."
  }
}

run "both_load_balancers_log_to_the_tls_only_bucket" {
  command = apply

  assert {
    condition = (
      aws_lb.frontend[0].access_logs[0].enabled && aws_lb.frontend[0].access_logs[0].prefix == "frontend" &&
      aws_lb.ui[0].access_logs[0].enabled && aws_lb.ui[0].access_logs[0].prefix == "ui"
    )
    error_message = "A load balancer does not write access logs."
  }

  assert {
    condition     = jsondecode(local.lb_log_policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" && jsondecode(local.lb_log_policy).Statement[0].Effect == "Deny"
    error_message = "The log bucket admits requests without TLS."
  }
}

run "an_operator_group_reaches_the_ui_on_443_only" {
  command = apply

  variables {
    ui_allowed_cidr_blocks         = []
    ui_operator_security_group_ids = ["sg-0jumphost"]
  }

  assert {
    condition = (
      length(aws_vpc_security_group_ingress_rule.alb_from_operators) == 0 &&
      aws_vpc_security_group_ingress_rule.alb_from_operator_groups[0].referenced_security_group_id == "sg-0jumphost" &&
      aws_vpc_security_group_ingress_rule.alb_from_operator_groups[0].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.alb_from_operator_groups[0].to_port == 443
    )
    error_message = "The UI load balancer admits a source other than the operator group, or a port other than 443."
  }

  assert {
    condition = (
      aws_vpc_security_group_egress_rule.operator_groups_to_alb[0].security_group_id == "sg-0jumphost" &&
      aws_vpc_security_group_egress_rule.operator_groups_to_alb[0].referenced_security_group_id == aws_security_group.alb[0].id &&
      aws_vpc_security_group_egress_rule.operator_groups_to_alb[0].from_port == 443
    )
    error_message = "The operator group cannot send HTTPS to the UI load balancer, or it can send to another destination."
  }
}

run "an_acm_frontend_terminates_tls_at_the_nlb" {
  command = apply

  variables {
    frontend_acm_certificate_arn = "arn:aws:acm:us-east-1:111111111111:certificate/11111111-2222-3333-4444-555555555555"
    frontend_server_name         = "temporal.example.com"
  }

  assert {
    condition = (
      aws_lb_listener.frontend[0].protocol == "TLS" && aws_lb_listener.frontend[0].alpn_policy == "HTTP2Only" &&
      aws_lb_listener.frontend[0].ssl_policy == "ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04" &&
      aws_lb_target_group.frontend[0].protocol == "TLS"
    )
    error_message = "The ACM frontend is not terminated under the FIPS policy with HTTP/2 and re-encrypted."
  }

  assert {
    condition = one([
      for variable in jsondecode(aws_ecs_task_definition.server[0].container_definitions)[1].environment :
      variable.value if variable.name == "TEMPORAL_TLS_FRONTEND_SERVER_NAME"
    ]) == "internode.temporal.internal"
    error_message = "Behind an ACM frontend the server does not verify its own frontend as the internode name."
  }
}

run "the_ui_needs_two_management_subnets" {
  command = plan

  variables {
    management_subnet_ids = ["subnet-mgmt-a"]
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "worker_versioning_v1_keys_are_refused" {
  command = plan

  variables {
    dynamic_config = { "frontend.workerVersioningDataAPIs" = [{ value = true }] }
  }

  expect_failures = [var.dynamic_config]
}
