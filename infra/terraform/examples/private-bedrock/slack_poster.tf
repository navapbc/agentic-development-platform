locals {
  slack_shared_app_enabled = local.eks_enabled && var.slack_shared_app != null
  slack_shared_app_environment_config = local.slack_shared_app_enabled ? {
    "slack-shared-app.yaml" = yamlencode({
      config_version  = 1
      app_id          = var.slack_shared_app.app_id
      organization_id = var.slack_shared_app.organization_id
      api_base_url    = var.slack_shared_app.api_base_url
    })
  } : {}
  slack_poster_workload = local.slack_shared_app_enabled ? {
    slack_poster = {
      service_account = "slack-poster"
      policy_arns     = []
      secrets         = ["slack_shared_bot_token", "slack_poster_tls"]
    }
  } : {}

  # One pattern covers every tenant's fingerprint secret, which the tenant module names
  # <name_prefix>/tenants/<tenant>/escalation-action-key-sha256, so the policy grows by one tenant
  # key ARN per tenant.
  slack_poster_fingerprint_pattern = "arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.name_prefix}/tenants/*/escalation-action-key-sha256-??????"

  slack_poster_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListTheTenantBundlesAndClaims"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = module.tenant_config_store.bucket_arn
        Condition = { StringLike = { "s3:prefix" = ["tenants/*/*", "claims/*/*/"] } }
      },
      {
        Sid    = "ReadTheSlackFiles"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = [
          "${module.tenant_config_store.bucket_arn}/tenants/*/escalation.yaml",
          "${module.tenant_config_store.bucket_arn}/tenants/*/gates.yaml",
        ]
      },
      {
        Sid      = "ReadTheClaimsProtocol"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${module.tenant_config_store.bucket_arn}/${module.tenant_config_store.claims_protocol_key}"
      },
      {
        Sid       = "DecryptTheTenantBundles"
        Effect    = "Allow"
        Action    = "kms:Decrypt"
        Resource  = module.tenant_config_store.kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
      },
      {
        Sid      = "ReadTheActionKeyFingerprints"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = local.slack_poster_fingerprint_pattern
      },
      {
        Sid      = "DecryptTheActionKeyFingerprints"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = [for name in sort(keys(local.tenant_records)) : local.tenant_records[name].kms_key_arn]
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" }
          StringLike   = { "kms:EncryptionContext:SecretARN" = local.slack_poster_fingerprint_pattern }
        }
      },
    ]
  })
}

resource "aws_iam_policy" "slack_poster" {
  count = local.slack_shared_app_enabled && length(local.tenants) > 0 ? 1 : 0

  name_prefix = "${var.name_prefix}-slack-poster-"
  description = "Lets the Slack posting service read each tenant's escalation.yaml, gates.yaml, and action key fingerprint, and list one claim's markers."
  policy      = local.slack_poster_policy

  tags = var.tags

  lifecycle {
    precondition {
      condition     = length(local.slack_poster_policy) <= 6144
      error_message = "The Slack posting service policy exceeds the 6144 character limit of a managed policy, toward which each tenant's key ARN counts."
    }
  }
}

resource "aws_iam_role_policy_attachment" "slack_poster" {
  count = local.slack_shared_app_enabled && length(local.tenants) > 0 ? 1 : 0

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["slack_poster"]), 1)
  policy_arn = aws_iam_policy.slack_poster[0].arn

  depends_on = [module.eks_cluster]
}
