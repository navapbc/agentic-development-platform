locals {
  # The add-ons whose pods are host-networked run before any node is Ready, and the nodes need
  # them to turn Ready. The others wait for nodes, because EKS reports them ACTIVE only once
  # their pods run.
  addons_before_nodes = {
    vpc-cni = jsonencode({
      enableNetworkPolicy = "true"
      env = {
        NETWORK_POLICY_ENFORCING_MODE = var.network_policy_enforcing_mode
        # Security groups for pods. The standard mode keeps NetworkPolicy and Pod Identity
        # working for a pod that has its own security group.
        ENABLE_POD_ENI                    = "true"
        POD_SECURITY_GROUP_ENFORCING_MODE = "standard"
      }
    })
    eks-pod-identity-agent = null
    kube-proxy             = null
  }

  addons_after_nodes = {
    coredns            = null
    aws-efs-csi-driver = null
    aws-secrets-store-csi-driver-provider = jsonencode({
      useFipsEndpoint = var.use_fips_endpoints
    })
  }
}

resource "aws_eks_addon" "before_nodes" {
  for_each = local.enabled ? local.addons_before_nodes : {}

  cluster_name                = aws_eks_cluster.this[0].name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, null)
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  dynamic "pod_identity_association" {
    for_each = { for name, identity in local.pod_identities : name => identity if identity.addon == each.key }
    content {
      service_account = pod_identity_association.value.service_account
      role_arn        = aws_iam_role.pod_identity[pod_identity_association.key].arn
    }
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy_attachment.pod_identity]
}

resource "aws_eks_addon" "after_nodes" {
  for_each = local.enabled ? local.addons_after_nodes : {}

  cluster_name                = aws_eks_cluster.this[0].name
  addon_name                  = each.key
  addon_version               = lookup(var.addon_versions, each.key, null)
  configuration_values        = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  dynamic "pod_identity_association" {
    for_each = { for name, identity in local.pod_identities : name => identity if identity.addon == each.key }
    content {
      service_account = pod_identity_association.value.service_account
      role_arn        = aws_iam_role.pod_identity[pod_identity_association.key].arn
    }
  }

  tags = var.tags

  depends_on = [
    aws_eks_node_group.this,
    aws_iam_role_policy_attachment.pod_identity,
  ]
}
