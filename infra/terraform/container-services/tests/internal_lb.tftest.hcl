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
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:loadbalancer/app/agentic-platform-internal/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:listener/app/agentic-platform-internal/1/2" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/agentic-platform/3" }
  }
}

override_resource {
  target = aws_security_group.internal_lb
  values = { id = "sg-0internallb" }
}

override_resource {
  target = aws_security_group.internal_client
  values = { id = "sg-0internalclient" }
}

variables {
  name_prefix                  = "agentic-platform"
  create_container_services    = true
  vpc_id                       = "vpc-1"
  application_subnet_ids       = ["subnet-a", "subnet-b"]
  access_log_bucket            = "agentic-platform-access-logs-111111111111"
  internal_acm_certificate_arn = "arn:aws:acm:us-east-1:111111111111:certificate/11111111-2222-3333-4444-555555555555"
}

run "no_certificate_creates_no_load_balancer" {
  command = plan

  variables {
    internal_acm_certificate_arn = ""
  }

  assert {
    condition     = length(aws_lb.internal) == 0 && length(aws_route53_zone.internal) == 0 && output.internal_endpoints == null
    error_message = "The internal load balancer was planned without a certificate."
  }
}

run "the_listener_terminates_tls_under_the_fips_policy" {
  command = apply

  assert {
    condition = (
      aws_lb.internal[0].internal && aws_lb.internal[0].drop_invalid_header_fields &&
      aws_lb_listener.internal[0].protocol == "HTTPS" &&
      aws_lb_listener.internal[0].ssl_policy == "ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04" &&
      aws_lb_listener.internal[0].default_action[0].type == "fixed-response"
    )
    error_message = "The internal listener is public, plain, outside the FIPS policy, or forwards unknown names."
  }

  assert {
    condition     = aws_lb.internal[0].access_logs[0].enabled && aws_lb.internal[0].access_logs[0].prefix == "internal"
    error_message = "The internal load balancer writes no access logs."
  }
}

run "every_service_is_reencrypted_under_its_own_name" {
  command = apply

  assert {
    condition = alltrue([
      for name, group in aws_lb_target_group.internal :
      group.protocol == "HTTPS" && group.health_check[0].protocol == "HTTPS"
    ])
    error_message = "A target group forwards to its tasks without TLS."
  }

  assert {
    condition     = aws_lb_target_group.internal["otel-collector"].protocol_version == "GRPC" && aws_lb_target_group.internal["coordination"].protocol_version == "HTTP1"
    error_message = "The collector target group does not speak gRPC, or another group does."
  }

  assert {
    condition = alltrue([
      for name, rule in aws_lb_listener_rule.internal :
      one(rule.condition[*].host_header[0].values) == toset(["${name}.platform.internal"]) &&
      rule.action[0].target_group_arn == aws_lb_target_group.internal[name].arn
    ])
    error_message = "A host rule names another service or forwards to another target group."
  }

  assert {
    condition     = length(distinct([for rule in aws_lb_listener_rule.internal : rule.priority])) == 4
    error_message = "Two host rules share a priority."
  }

  assert {
    condition     = output.internal_endpoints.urls["coordination"] == "https://coordination.platform.internal"
    error_message = "The coordination URL is not its private name over HTTPS."
  }
}

run "each_target_admits_only_the_load_balancer" {
  command = apply

  variables {
    internal_allowed_cidr_blocks = ["10.0.21.0/24"]
  }

  assert {
    condition = alltrue([
      for name, rule in aws_vpc_security_group_ingress_rule.target_from_internal_lb :
      rule.referenced_security_group_id == "sg-0internallb" && rule.from_port == local.internal_services[name].port && rule.to_port == rule.from_port
    ])
    error_message = "A target group admits a source other than the load balancer, or another port."
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.internal_lb_from_clients[0].referenced_security_group_id == "sg-0internalclient" &&
      aws_vpc_security_group_ingress_rule.internal_lb_from_clients[0].from_port == 443 &&
      keys(aws_vpc_security_group_ingress_rule.internal_lb_from_networks) == ["10.0.21.0/24"]
    )
    error_message = "The load balancer admits a source other than its client group and the allowed networks."
  }
}

run "a_load_balancer_without_its_network_is_refused" {
  command = plan

  variables {
    application_subnet_ids = ["subnet-a"]
  }

  expect_failures = [terraform_data.internal_lb_guard]
}
