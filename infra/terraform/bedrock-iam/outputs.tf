output "invoke_policy_arn" {
  description = "ARN of the bedrock:InvokeModel policy, or null when not created."
  value       = try(aws_iam_policy.invoke[0].arn, null)
}
