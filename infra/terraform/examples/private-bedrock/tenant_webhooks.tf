locals {
  tenant_webhooks = length(local.tenants) > 1

  # One policy covers every tenant, so the inbound webhook role holds one managed policy whatever
  # the tenant count. The secret patterns match only the webhook signing secrets the tenant module
  # names <name_prefix>/tenants/<tenant>/<key>, and the tenant keys decrypt only those secrets.
  # The policy holds about 1700 characters plus one key ARN of up to 90 characters per tenant, so
  # it stays under the 6144 character limit of a managed policy up to about 50 tenants.
  tenant_webhook_records = { for name, tenant in module.tenant : name => tenant.webhook if local.tenant_webhooks }
  tenant_webhook_secret_patterns = [
    for key in sort(distinct(flatten([for webhook in values(local.tenant_webhook_records) : keys(webhook.secret_arns)]))) :
    "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name_prefix}/tenants/*/${replace(key, "_", "-")}-??????"
  ]
  tenant_webhook_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheTenantWebhookSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = local.tenant_webhook_secret_patterns
      },
      {
        Sid      = "DecryptTheTenantWebhookSecrets"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = [for name in sort(keys(local.tenant_webhook_records)) : local.tenant_webhook_records[name].kms_key_arn]
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" }
          StringLike   = { "kms:EncryptionContext:SecretARN" = local.tenant_webhook_secret_patterns }
        }
      },
      {
        Sid       = "ListTheTenantBundles"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = module.tenant_config_store.bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["tenants/*/*"] } }
      },
      {
        Sid      = "ReadTheTenantBundles"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${module.tenant_config_store.bucket_arn}/tenants/*"
      },
      {
        Sid       = "DecryptTheTenantBundles"
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = module.tenant_config_store.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_iam_policy" "tenant_webhook" {
  count = local.tenant_webhooks ? 1 : 0

  name_prefix = "${var.name_prefix}-tenant-webhooks-"
  description = "Lets the platform's inbound webhook read every tenant's webhook signing secrets and config bundle."
  policy      = local.tenant_webhook_policy

  tags = var.tags

  lifecycle {
    precondition {
      condition     = length(local.tenant_webhook_policy) <= 6144
      error_message = "The inbound webhook policy exceeds the 6144 character limit of a managed policy, toward which each tenant's key ARN counts."
    }
  }
}

resource "aws_iam_role_policy_attachment" "tenant_webhook" {
  count = local.tenant_webhooks ? 1 : 0

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["inbound_webhook"]), 1)
  policy_arn = aws_iam_policy.tenant_webhook[0].arn

  depends_on = [module.eks_cluster]
}
