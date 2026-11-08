data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  enabled      = var.create_eks_cluster
  cluster_name = var.cluster_name != "" ? var.cluster_name : "${var.name_prefix}-eks"
  account_id   = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  region       = data.aws_region.current.region

  cluster_arn    = "arn:${local.partition}:eks:${local.region}:${local.account_id}:cluster/${local.cluster_name}"
  log_group_name = "/aws/eks/${local.cluster_name}/cluster"
}

resource "aws_cloudwatch_log_group" "cluster" {
  count = local.enabled ? 1 : 0

  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs[0].arn

  tags = merge(var.tags, { Name = "${local.cluster_name}-control-plane" })
}

resource "aws_eks_cluster" "this" {
  count = local.enabled ? 1 : 0

  name     = local.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster[0].arn

  enabled_cluster_log_types     = ["api", "audit", "authenticator"]
  bootstrap_self_managed_addons = false

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = false
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.secrets[0].arn
    }
  }

  kubernetes_network_config {
    ip_family = "ipv4"
  }

  upgrade_policy {
    support_type = "STANDARD"
  }

  tags = merge(var.tags, { Name = local.cluster_name })

  lifecycle {
    precondition {
      condition     = length(var.subnet_ids) >= 2
      error_message = "subnet_ids must list at least two subnets in different Availability Zones."
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.cluster,
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_secrets_key,
  ]
}

resource "aws_vpc_security_group_ingress_rule" "api_access" {
  count = local.enabled ? length(var.api_access_security_group_ids) : 0

  security_group_id            = aws_eks_cluster.this[0].vpc_config[0].cluster_security_group_id
  referenced_security_group_id = var.api_access_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Kubernetes API from an operator security group"

  tags = var.tags
}

resource "aws_vpc_security_group_egress_rule" "api_access" {
  count = local.enabled ? length(var.api_access_security_group_ids) : 0

  security_group_id            = var.api_access_security_group_ids[count.index]
  referenced_security_group_id = aws_eks_cluster.this[0].vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Kubernetes API of ${local.cluster_name}"

  tags = var.tags
}

resource "aws_eks_access_entry" "admin" {
  for_each = local.enabled ? var.admin_principal_arns : {}

  cluster_name  = aws_eks_cluster.this[0].name
  principal_arn = each.value
  type          = "STANDARD"

  tags = var.tags
}

resource "aws_eks_access_policy_association" "admin" {
  for_each = aws_eks_access_entry.admin

  cluster_name  = aws_eks_cluster.this[0].name
  principal_arn = each.value.principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}
