locals {
  enabled = var.create_dns_firewall
  domains = distinct(concat(var.allowed_domains, var.additional_allowed_domains))

  # Route 53 Resolver stores each domain as a fully qualified name, so a name without the trailing dot plans a change on every run.
  fqdns = [for domain in local.domains : endswith(domain, ".") ? domain : "${domain}."]
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.enabled || var.vpc_id != ""
      error_message = "create_dns_firewall requires vpc_id."
    }
  }
}

resource "aws_route53_resolver_firewall_domain_list" "allowed" {
  count = local.enabled ? 1 : 0

  name    = "${var.name_prefix}-allowed"
  domains = local.fqdns
  tags    = var.tags
}

resource "aws_route53_resolver_firewall_domain_list" "everything" {
  count = local.enabled ? 1 : 0

  name    = "${var.name_prefix}-everything"
  domains = ["*."]
  tags    = var.tags
}

resource "aws_route53_resolver_firewall_rule_group" "egress" {
  count = local.enabled ? 1 : 0

  name = "${var.name_prefix}-egress"
  tags = var.tags
}

resource "aws_route53_resolver_firewall_rule" "allow_listed" {
  count = local.enabled ? 1 : 0

  name                    = "allow-listed"
  action                  = "ALLOW"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.allowed[0].id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.egress[0].id
  priority                = 100
}

resource "aws_route53_resolver_firewall_rule" "default" {
  count = local.enabled ? 1 : 0

  name                    = "default-${lower(var.default_action)}"
  action                  = var.default_action
  block_response          = var.default_action == "BLOCK" ? "NXDOMAIN" : null
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.everything[0].id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.egress[0].id
  priority                = 200
}

resource "aws_route53_resolver_firewall_rule_group_association" "vpc" {
  count = local.enabled ? 1 : 0

  name                   = "${var.name_prefix}-egress"
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.egress[0].id
  vpc_id                 = var.vpc_id
  priority               = 101
  tags                   = var.tags

  depends_on = [
    aws_route53_resolver_firewall_rule.allow_listed,
    aws_route53_resolver_firewall_rule.default,
  ]
}

resource "aws_route53_resolver_firewall_config" "vpc" {
  count = local.enabled ? 1 : 0

  resource_id        = var.vpc_id
  firewall_fail_open = "DISABLED"
}

resource "aws_cloudwatch_log_group" "queries" {
  count = local.enabled ? 1 : 0

  name              = "/aws/route53resolver/${var.name_prefix}"
  retention_in_days = var.query_log_retention_days
  kms_key_id        = var.kms_key_arn == "" ? null : var.kms_key_arn
  tags              = var.tags
}

resource "aws_route53_resolver_query_log_config" "vpc" {
  count = local.enabled ? 1 : 0

  name            = "${var.name_prefix}-queries"
  destination_arn = aws_cloudwatch_log_group.queries[0].arn
  tags            = var.tags
}

resource "aws_route53_resolver_query_log_config_association" "vpc" {
  count = local.enabled ? 1 : 0

  resolver_query_log_config_id = aws_route53_resolver_query_log_config.vpc[0].id
  resource_id                  = var.vpc_id
}
