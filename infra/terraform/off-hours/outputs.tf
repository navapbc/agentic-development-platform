output "function_role_arn" {
  description = "Role the off-hours function runs as, which reads the monitor secret. Null when disabled."
  value       = try(aws_iam_role.function[0].arn, null)
}

output "function_name" {
  description = "Lambda function that runs the off-hours steps. Null when disabled."
  value       = try(aws_lambda_function.scale[0].function_name, null)
}

output "function_arn" {
  description = "Lambda function that runs the off-hours steps, for a run outside the schedule with aws lambda invoke and for the CloudTrail data-event selector. Null when disabled."
  value       = local.enabled ? local.function_arn : null
}

output "dead_letter_queue_url" {
  description = "SQS queue holding each schedule delivery the scheduler could not hand to the function. Null when disabled."
  value       = try(aws_sqs_queue.dead_letters[0].url, null)
}

output "wake_counts_parameter" {
  description = "SSM parameter holding the counts the wake restores. Null when disabled."
  value       = try(aws_ssm_parameter.wake_counts[0].name, null)
}

output "alert_topic_arn" {
  description = "SNS topic a failed check or run publishes to, naming each running service, pod, and connection. Null when disabled."
  value       = try(aws_sns_topic.alerts[0].arn, null)
}

output "metric_namespace" {
  description = "CloudWatch namespace of the Completed, Failed, and CheckFindings metrics. Null when disabled."
  value       = local.enabled ? local.metric_namespace : null
}
