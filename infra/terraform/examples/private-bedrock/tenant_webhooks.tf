locals {
  tenant_webhooks = length(local.tenants) > 1
}

resource "aws_iam_role_policy_attachment" "tenant_webhook" {
  for_each = local.tenant_webhooks ? local.tenants : {}

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["inbound_webhook"]), 1)
  policy_arn = module.tenant[each.key].webhook.reader_policy_arn

  depends_on = [module.eks_cluster]
}
