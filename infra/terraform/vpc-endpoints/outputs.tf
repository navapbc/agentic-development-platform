output "security_group_id" {
  description = "Shared endpoint security group id, or null when disabled."
  value       = try(aws_security_group.endpoints[0].id, null)
}

output "interface_endpoint_ids" {
  description = "Map of interface service name to endpoint id."
  value       = { for name, ep in aws_vpc_endpoint.interface : name => ep.id }
}

output "s3_gateway_endpoint_id" {
  description = "S3 gateway endpoint id, or null when not created."
  value       = try(aws_vpc_endpoint.s3[0].id, null)
}
