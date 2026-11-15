output "documents" {
  description = "Automation documents that put the services to sleep and wake them, for a run outside the schedule with aws ssm start-automation-execution. Empty when disabled."
  value       = { for name, document in aws_ssm_document.scale : name => document.name }
}

output "automation_role_arn" {
  description = "Role the documents assume, the AutomationAssumeRole of a run outside the schedule. Null when disabled."
  value       = try(aws_iam_role.automation[0].arn, null)
}

output "wake_counts_parameter" {
  description = "SSM parameter holding the counts the wake restores. Null when disabled."
  value       = try(aws_ssm_parameter.wake_counts[0].name, null)
}
