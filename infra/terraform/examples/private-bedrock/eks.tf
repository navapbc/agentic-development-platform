locals {
  eks_enabled = var.create_eks

  # Secret store keys each EKS workload reads through the Secrets Store CSI driver. The database
  # keys exist only with Aurora, and the secret store rejects a reader key that names no secret.
  eks_workloads = {
    activity_worker = {
      service_account = "activity-worker"
      policy_arns     = []
      secrets = [
        "database_platform_app", "coordination_shared_secret", "litellm_master_key", "authorship_signing_key",
        "review_attestation_secret", "github_token", "jira_api_token", "jira_pat", "slack_webhook_url",
        "langfuse_public_key", "langfuse_secret_key",
      ]
    }
    litellm = {
      service_account = "litellm"
      policy_arns     = [module.bedrock_iam.invoke_policy_arn]
      secrets         = ["litellm_master_key", "langfuse_public_key", "langfuse_secret_key"]
    }
    mcp_gateway = {
      service_account = "mcp-gateway"
      policy_arns     = []
      secrets         = ["database_platform_app", "mcp_gateway_token"]
    }
    inbound_webhook = {
      service_account = "inbound-webhook"
      policy_arns     = []
      secrets         = ["database_platform_app", "github_webhook_secret", "jira_webhook_secret"]
    }
  }

  eks_secret_keys = distinct(flatten([
    for workload in values(local.eks_workloads) : [
      for key in workload.secrets : key if !startswith(key, "database_") || local.secret_store_database
    ]
  ]))
  eks_secret_readers = local.eks_enabled && var.create_secret_store ? {
    for key in local.eks_secret_keys : key => [
      for name, workload in local.eks_workloads : module.eks_cluster.pod_identity_role_arns[name] if contains(workload.secrets, key)
    ]
  } : {}

  # The Pod Identity agent reaches the eks-auth interface endpoint under this name.
  eks_domains = local.eks_enabled ? ["eks-auth.${var.region}.api.aws"] : []

  eks_activity_worker_role_arns = local.eks_enabled ? [module.eks_cluster.pod_identity_role_arns["activity_worker"]] : []
  eks_workspace                 = local.eks_enabled && module.container_services.workspace != null
}

resource "aws_iam_policy" "eks_workspace_mount" {
  count = local.eks_workspace ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-workspace-mount-"
  description = "Lets the EFS CSI node driver mount the workspace checkouts access point read-write for the EKS pods."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "MountTheCheckoutsAccessPoint"
      Effect   = "Allow"
      Action   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
      Resource = module.container_services.workspace.file_system_arn
      Condition = {
        StringEquals = { "elasticfilesystem:AccessPointArn" = module.container_services.workspace.access_point_arns["checkouts"] }
      }
    }]
  })

  tags = var.tags
}

module "eks_cluster" {
  source = "../../eks-cluster"

  create_eks_cluster            = local.eks_enabled
  name_prefix                   = var.name_prefix
  kubernetes_version            = var.eks_kubernetes_version
  subnet_ids                    = local.subnet_ids
  api_access_security_group_ids = [module.bedrock_jumphost.task_security_group_id]
  admin_principal_arns          = var.eks_admin_principal_arns
  platform_node_group           = var.eks_platform_node_group
  sandbox_node_group            = var.eks_sandbox_node_group
  network_policy_enforcing_mode = var.eks_network_policy_enforcing_mode
  use_fips_endpoints            = var.container_use_fips_endpoints
  addon_versions                = var.eks_addon_versions

  workloads                = { for name, workload in local.eks_workloads : name => { service_account = workload.service_account, policy_arns = workload.policy_arns } }
  efs_csi_node_policy_arns = aws_iam_policy.eks_workspace_mount[*].arn

  tags = var.tags
}
