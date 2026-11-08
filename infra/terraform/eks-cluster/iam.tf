locals {
  managed_policy_prefix = "arn:${local.partition}:iam::aws:policy"

  # addon names the managed add-on whose pod_identity_association binds the role. The other
  # roles are bound by an aws_eks_pod_identity_association.
  pod_identities = {
    activity_worker = {
      namespace       = var.platform_namespace
      service_account = "activity-worker"
      addon           = null
      policy_arns     = var.activity_worker_policy_arns
    }
    litellm = {
      namespace       = var.platform_namespace
      service_account = "litellm"
      addon           = null
      policy_arns     = var.litellm_policy_arns
    }
    load_balancer_controller = {
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
      addon           = null
      policy_arns     = []
    }
    vpc_cni = {
      namespace       = "kube-system"
      service_account = "aws-node"
      addon           = "vpc-cni"
      policy_arns     = ["${local.managed_policy_prefix}/AmazonEKS_CNI_Policy"]
    }
    efs_csi_controller = {
      namespace       = "kube-system"
      service_account = "efs-csi-controller-sa"
      addon           = "aws-efs-csi-driver"
      policy_arns     = ["${local.managed_policy_prefix}/service-role/AmazonEFSCSIDriverPolicy"]
    }
    efs_csi_node = {
      namespace       = "kube-system"
      service_account = "efs-csi-node-sa"
      addon           = "aws-efs-csi-driver"
      policy_arns     = var.efs_csi_node_policy_arns
    }
  }

  pod_identity_policy_attachments = merge([
    for name, identity in local.pod_identities : {
      for index, arn in identity.policy_arns : "${name}-${index}" => { identity = name, policy_arn = arn }
    }
  ]...)
}

resource "aws_iam_role" "cluster" {
  count = local.enabled ? 1 : 0

  name = "${local.cluster_name}-cluster"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each = toset(local.enabled ? ["AmazonEKSClusterPolicy", "AmazonEKSVPCResourceController"] : [])

  role       = aws_iam_role.cluster[0].name
  policy_arn = "${local.managed_policy_prefix}/${each.key}"
}

resource "aws_iam_role_policy" "cluster_secrets_key" {
  count = local.enabled ? 1 : 0

  name = "secrets-key"
  role = aws_iam_role.cluster[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "EnvelopeEncryptKubernetesSecrets"
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ListGrants", "kms:DescribeKey"]
      Resource = aws_kms_key.secrets[0].arn
    }]
  })
}

resource "aws_iam_role" "node" {
  count = local.enabled ? 1 : 0

  name = "${local.cluster_name}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset(local.enabled ? ["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"] : [])

  role       = aws_iam_role.node[0].name
  policy_arn = "${local.managed_policy_prefix}/${each.key}"
}

resource "aws_iam_role" "pod_identity" {
  for_each = local.enabled ? local.pod_identities : {}

  name = "${local.cluster_name}-${replace(each.key, "_", "-")}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:SourceAccount"                         = local.account_id
          "aws:RequestTag/kubernetes-namespace"       = each.value.namespace
          "aws:RequestTag/kubernetes-service-account" = each.value.service_account
        }
        ArnEquals = { "aws:SourceArn" = local.cluster_arn }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "pod_identity" {
  for_each = local.enabled ? local.pod_identity_policy_attachments : {}

  role       = aws_iam_role.pod_identity[each.value.identity].name
  policy_arn = each.value.policy_arn
}

resource "aws_iam_role_policy" "load_balancer_controller" {
  count = local.enabled ? 1 : 0

  name   = "load-balancer-controller"
  role   = aws_iam_role.pod_identity["load_balancer_controller"].id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/load-balancer-controller.json.tftpl", { partition = local.partition })))
}

resource "aws_eks_pod_identity_association" "this" {
  for_each = { for name, identity in local.pod_identities : name => identity if local.enabled && identity.addon == null }

  cluster_name    = aws_eks_cluster.this[0].name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.pod_identity[each.key].arn

  tags = var.tags
}
