output "file_system_id" {
  description = "EFS file system id for the ClickHouse task volume, or null when the module is disabled."
  value       = try(aws_efs_file_system.this[0].id, null)
}

output "file_system_arn" {
  description = "EFS file system ARN, used in the task role's elasticfilesystem client permissions."
  value       = try(aws_efs_file_system.this[0].arn, null)
}

output "access_point_id" {
  description = "Access point the ClickHouse task mounts. The file system policy denies mounts outside it."
  value       = try(aws_efs_access_point.clickhouse[0].id, null)
}

output "access_point_arn" {
  description = "Access point ARN."
  value       = try(aws_efs_access_point.clickhouse[0].arn, null)
}

output "security_group_id" {
  description = "Mount target security group id, or null when the module is disabled."
  value       = try(aws_security_group.this[0].id, null)
}
