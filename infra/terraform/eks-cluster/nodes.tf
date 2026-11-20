locals {
  node_pool_label = "agentic-dev-platform/node-pool"
  sandbox_taint   = { key = "agentic-dev-platform/sandbox", value = "true", effect = "NO_SCHEDULE" }

  node_groups = {
    platform = var.platform_node_group
    sandbox  = var.sandbox_node_group
  }
}

resource "aws_launch_template" "node" {
  for_each = local.enabled ? local.node_groups : {}

  name_prefix            = "${local.cluster_name}-${each.key}-"
  description            = "Nodes of the ${each.key} node group of ${local.cluster_name}."
  update_default_version = true

  # A hop limit of 1 keeps pods that are not host-networked from reaching the instance
  # metadata service and the node role. Pods receive credentials through Pod Identity.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = each.value.disk_size_gib
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.ebs[0].arn
      delete_on_termination = true
    }
  }

  dynamic "tag_specifications" {
    for_each = ["instance", "volume", "network-interface"]
    content {
      resource_type = tag_specifications.value
      tags          = merge(var.tags, { Name = "${local.cluster_name}-${each.key}" })
    }
  }

  tags = var.tags
}

resource "aws_eks_node_group" "this" {
  for_each = local.enabled ? local.node_groups : {}

  cluster_name    = aws_eks_cluster.this[0].name
  node_group_name = "${local.cluster_name}-${each.key}"
  node_role_arn   = aws_iam_role.node[0].arn
  subnet_ids      = var.subnet_ids
  version         = var.kubernetes_version
  ami_type        = "AL2023_ARM_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  instance_types  = each.value.instance_types

  scaling_config {
    min_size     = each.value.min_size
    max_size     = each.value.max_size
    desired_size = each.value.desired_size
  }

  update_config {
    max_unavailable = 1
  }

  launch_template {
    id      = aws_launch_template.node[each.key].id
    version = aws_launch_template.node[each.key].latest_version
  }

  labels = { (local.node_pool_label) = each.key }

  dynamic "taint" {
    for_each = each.key == "sandbox" ? [local.sandbox_taint] : []
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  tags = merge(var.tags, { Name = "${local.cluster_name}-${each.key}" })

  # A node turns Ready only once the VPC CNI runs on it, and the CNI reads its credentials
  # through the Pod Identity agent.
  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.before_nodes,
  ]
}
