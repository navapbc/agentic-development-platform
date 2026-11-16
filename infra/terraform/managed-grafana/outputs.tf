output "workspace_id" {
  description = "ID of the Grafana workspace. Null when disabled."
  value       = try(aws_grafana_workspace.this[0].id, null)
}

output "workspace_url" {
  description = "URL operators open the workspace under, after IAM Identity Center sign-in. Null when disabled."
  value       = local.enabled ? "https://${aws_grafana_workspace.this[0].endpoint}" : null
}

output "workspace_role_arn" {
  description = "Workspace role, which reads X-Ray and assumes the query role. Null when disabled."
  value       = try(aws_iam_role.workspace[0].arn, null)
}

output "query_role_arn" {
  description = "Role that may query the Prometheus workspace and may not write to it. Null when disabled."
  value       = try(aws_iam_role.query[0].arn, null)
}

output "security_group_id" {
  description = "Security group of the workspace network interfaces. Null when disabled."
  value       = try(aws_security_group.workspace[0].id, null)
}

output "kms_key_arn" {
  description = "KMS key that encrypts the workspace. Null when disabled or in AWS GovCloud (US), where the workspace uses an AWS owned key."
  value       = try(aws_kms_key.workspace[0].arn, null)
}

output "data_sources" {
  description = "Settings a Grafana Admin enters for the Prometheus and X-Ray data sources. Null when disabled."
  value = local.enabled ? {
    prometheus = {
      url             = trimsuffix(var.prometheus_endpoint, "/")
      auth            = "SigV4, Workspace IAM role"
      region          = local.region
      assume_role_arn = aws_iam_role.query[0].arn
    }
    xray = {
      auth   = "Workspace IAM role"
      region = local.region
    }
  } : null
}

output "interface_services" {
  description = "Interface endpoint services the workspace reaches AWS through, for the caller's vpc-endpoints module. The workspace calls the xray name, so the list holds it where the collector uses xray-fips. Empty when disabled."
  value       = local.enabled ? ["aps-workspaces", "grafana-workspace", "sts", "xray"] : []
}
