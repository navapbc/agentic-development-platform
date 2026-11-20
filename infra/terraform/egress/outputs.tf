output "egress_mode" {
  description = "Egress path in effect."
  value       = var.egress_mode
}

output "public_ip" {
  description = "Public address that outbound traffic from the private tiers appears from, or null when egress_mode is none. Allowlist it where a destination filters by source address."
  value       = try(aws_eip.nat[0].public_ip, null)
}

output "nat_instance_id" {
  description = "NAT instance id when egress_mode is nat_instance, else null."
  value       = try(aws_instance.nat[0].id, null)
}

output "nat_gateway_id" {
  description = "NAT Gateway id when egress_mode is nat_gateway, else null."
  value       = try(aws_nat_gateway.this[0].id, null)
}

output "nat_instance_security_group_id" {
  description = "NAT instance security group id when egress_mode is nat_instance, else null."
  value       = try(aws_security_group.nat_instance[0].id, null)
}
