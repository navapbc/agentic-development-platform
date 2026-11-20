output "cluster_id" {
  description = "Cluster identifier, or null when the module is disabled."
  value       = try(aws_rds_cluster.this[0].id, null)
}

output "cluster_arn" {
  description = "Cluster ARN, or null when the module is disabled."
  value       = try(aws_rds_cluster.this[0].arn, null)
}

output "cluster_resource_id" {
  description = "Cluster resource id, used in rds-db:connect IAM policies for IAM database authentication."
  value       = try(aws_rds_cluster.this[0].cluster_resource_id, null)
}

output "endpoint" {
  description = "Writer endpoint."
  value       = try(aws_rds_cluster.this[0].endpoint, null)
}

output "reader_endpoint" {
  description = "Reader endpoint. It resolves to the writer while instance_count is 1."
  value       = try(aws_rds_cluster.this[0].reader_endpoint, null)
}

output "port" {
  description = "Database port."
  value       = var.port
}

output "database_name" {
  description = "Initial database created with the cluster."
  value       = var.database_name
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN of the RDS-managed master user secret. Reserve it for administration and role bootstrap."
  value       = try(aws_rds_cluster.this[0].master_user_secret[0].secret_arn, null)
}

output "security_group_id" {
  description = "Cluster security group id, or null when the module is disabled."
  value       = try(aws_security_group.this[0].id, null)
}
