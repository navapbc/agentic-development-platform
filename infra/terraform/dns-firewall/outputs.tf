output "rule_group_id" {
  description = "Firewall rule group id, or null when the module is disabled."
  value       = try(aws_route53_resolver_firewall_rule_group.egress[0].id, null)
}

output "allowed_domains" {
  description = "Domains the VPC may resolve."
  value       = local.enabled ? local.domains : []
}

output "query_log_group_name" {
  description = "CloudWatch log group holding the resolver query log, or null when the module is disabled."
  value       = try(aws_cloudwatch_log_group.queries[0].name, null)
}
