output "cluster_name" {
  description = "Name of the EKS cluster."
  value       = local.enabled ? aws_eks_cluster.this[0].name : ""
}

output "cluster_arn" {
  description = "ARN of the EKS cluster."
  value       = local.enabled ? aws_eks_cluster.this[0].arn : ""
}

output "cluster_endpoint" {
  description = "Private endpoint of the Kubernetes API."
  value       = local.enabled ? aws_eks_cluster.this[0].endpoint : ""
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded CA certificate of the Kubernetes API."
  value       = local.enabled ? aws_eks_cluster.this[0].certificate_authority[0].data : ""
}

output "cluster_security_group_id" {
  description = "Security group EKS creates for the control plane and the nodes. A data store admits the pods through it."
  value       = local.enabled ? aws_eks_cluster.this[0].vpc_config[0].cluster_security_group_id : ""
}

output "vpc_id" {
  description = "VPC of the cluster, which the Load Balancer Controller needs because the hop limit of 1 keeps it from reading the instance metadata."
  value       = local.enabled ? aws_eks_cluster.this[0].vpc_config[0].vpc_id : ""
}

output "node_role_arn" {
  description = "Role of the managed nodes."
  value       = local.enabled ? aws_iam_role.node[0].arn : ""
}

output "pod_identity_role_arns" {
  description = "Pod Identity role of each workload, keyed by each workload name and by load_balancer_controller, vpc_cni, efs_csi_controller, and efs_csi_node. A secret store lists a role as a reader to let the pod read the secret."
  value       = { for name, role in aws_iam_role.pod_identity : name => role.arn }
}

output "secrets_kms_key_arn" {
  description = "KMS key that envelope-encrypts the Kubernetes secrets."
  value       = local.enabled ? aws_kms_key.secrets[0].arn : ""
}

output "ebs_kms_key_arn" {
  description = "KMS key that encrypts the node volumes."
  value       = local.enabled ? aws_kms_key.ebs[0].arn : ""
}

output "log_group_name" {
  description = "CloudWatch log group of the control plane logs."
  value       = local.enabled ? aws_cloudwatch_log_group.cluster[0].name : ""
}

output "node_pool_label" {
  description = "Node label whose value is platform or sandbox, for a pod's nodeSelector."
  value       = local.node_pool_label
}

output "sandbox_taint" {
  description = "Taint of the sandbox node group, which the sandbox, gate, checks, and scanner pods tolerate. The effect is in the EKS API form."
  value       = local.sandbox_taint
}

output "load_balancer_controller" {
  description = "Settings for the AWS Load Balancer Controller chart install. The role policy is vendored from app_version."
  value = local.enabled ? {
    app_version     = "v3.6.2"
    namespace       = local.pod_identities.load_balancer_controller.namespace
    service_account = local.pod_identities.load_balancer_controller.service_account
    role_arn        = aws_iam_role.pod_identity["load_balancer_controller"].arn
  } : null
}
