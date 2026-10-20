output "vpc_id" {
  description = "VPC the resources run in, created or supplied."
  value       = local.vpc_id
}

output "subnet_ids" {
  description = "Private subnets the resources run in, created or supplied."
  value       = local.subnet_ids
}

output "jumphost_ecr_repository_url" {
  description = "Repository to push the jumphost image to before connecting."
  value       = module.jumphost_ecr.repository_url
}

output "jumphost_cluster_name" {
  description = "ECS cluster the connect script runs a task on."
  value       = module.bedrock_jumphost.cluster_name
}

output "jumphost_ssm_param_prefix" {
  description = "SSM parameter prefix the connect script resolves."
  value       = module.bedrock_jumphost.ssm_param_prefix
}

output "bedrock_invoke_policy_arn" {
  description = "Attach this to the workload role that signs Bedrock inference."
  value       = module.bedrock_iam.invoke_policy_arn
}

output "vpc_endpoints_security_group_id" {
  description = "Shared endpoint security group id."
  value       = module.vpc_endpoints.security_group_id
}

output "management_subnet_ids" {
  description = "Management tier subnets, where the jumphost and administrative tasks run."
  value       = local.management_subnet_ids
}

output "data_subnet_ids" {
  description = "Data tier subnets. They have no default route and a network ACL that admits only the application and management tiers."
  value       = local.data_subnet_ids
}

output "egress_mode" {
  description = "Outbound internet path in effect."
  value       = module.egress.egress_mode
}

output "egress_public_ip" {
  description = "Address outbound traffic appears from, for destinations that filter by source address."
  value       = module.egress.public_ip
}

output "dns_firewall_allowed_domains" {
  description = "Domains the VPC may resolve when the DNS Firewall is enabled."
  value       = module.dns_firewall.allowed_domains
}

output "aurora_endpoint" {
  description = "Aurora writer endpoint, or null when create_aurora is false."
  value       = module.aurora_postgresql.endpoint
}

output "aurora_master_user_secret_arn" {
  description = "RDS-managed master user secret. Reserve it for administration and role bootstrap."
  value       = module.aurora_postgresql.master_user_secret_arn
}

output "cache_endpoint" {
  description = "Valkey primary endpoint, or null when create_cache is false."
  value       = module.elasticache.primary_endpoint_address
}

output "cache_user_secret_arn" {
  description = "Secret holding the Valkey RBAC user credential for Langfuse."
  value       = module.elasticache.user_secret_arn
}

output "clickhouse_file_system_id" {
  description = "EFS file system backing the ClickHouse task, or null when create_clickhouse_storage is false."
  value       = module.clickhouse_storage.file_system_id
}

output "clickhouse_access_point_id" {
  description = "EFS access point the ClickHouse task mounts."
  value       = module.clickhouse_storage.access_point_id
}

output "langfuse_bucket" {
  description = "Langfuse event and media bucket, or null when create_langfuse_bucket is false."
  value       = module.langfuse_blob_store.bucket_id
}
