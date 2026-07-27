output "vpc_endpoint_id" {
  description = "Interface endpoint id, or null when the module is disabled."
  value       = try(aws_vpc_endpoint.bedrock_runtime[0].id, null)
}

output "endpoint_dns_names" {
  description = "Private DNS entries for the endpoint (empty when private DNS is enabled and callers use the regional name)."
  value       = try(aws_vpc_endpoint.bedrock_runtime[0].dns_entry[*].dns_name, [])
}

output "security_group_id" {
  description = "Security group id guarding the endpoint, or null when disabled."
  value       = try(aws_security_group.bedrock_vpce[0].id, null)
}

output "invoke_policy_arn" {
  description = "ARN of the bedrock:InvokeModel policy, or null when not created."
  value       = try(aws_iam_policy.bedrock_invoke[0].arn, null)
}
