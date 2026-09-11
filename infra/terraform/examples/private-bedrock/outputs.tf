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
