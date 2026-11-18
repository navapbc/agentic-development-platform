variable "create_webhook_secrets" {
  description = "Create the tenant's GitHub and Jira webhook signing secrets, which only webhook_reader_arn reads. The platform's inbound webhook verifies the tenant's deliveries with them when it serves more than one tenant, and the deployment grants its role every tenant's secrets, keys, and bundles in one policy."
  type        = bool
  default     = false
}

variable "webhook_reader_arn" {
  description = "Pod Identity role ARN of the platform's inbound webhook, the one principal that reads the tenant's webhook signing secrets. Required with create_webhook_secrets."
  type        = string
  default     = ""
}

locals {
  webhook_enabled      = local.enabled && var.create_webhook_secrets
  webhook_secret_keys  = ["github_webhook_secret", "jira_webhook_secret"]
  webhook_secret_names = local.webhook_enabled ? toset(local.webhook_secret_keys) : toset([])
}

resource "terraform_data" "webhook_guard" {
  count = local.webhook_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.webhook_reader_arn != ""
      error_message = "create_webhook_secrets needs webhook_reader_arn, the Pod Identity role of the platform's inbound webhook."
    }
    precondition {
      condition     = length(setintersection(toset(var.secrets), toset(local.webhook_secret_keys))) == 0
      error_message = "secrets may not name github_webhook_secret or jira_webhook_secret, which create_webhook_secrets creates for the inbound webhook alone."
    }
  }
}

# Each secret holds the value the tenant's GitHub or Jira webhook signs with. An operator stores
# it, and until then the deployment mounts no value and the webhook refuses the tenant's deliveries.
resource "aws_secretsmanager_secret" "webhook" {
  for_each = local.webhook_secret_names

  name                    = "${local.secret_prefix}${replace(each.key, "_", "-")}"
  description             = "${each.key} of the ${var.tenant} tenant, which only the platform's inbound webhook reads to verify the tenant's deliveries."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

resource "aws_secretsmanager_secret_policy" "webhook" {
  for_each = aws_secretsmanager_secret.webhook

  secret_arn          = each.value.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheInboundWebhook"
        Effect    = "Allow"
        Principal = { AWS = var.webhook_reader_arn }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.webhook_reader_arn } }
      },
    ]
  })
}

output "webhook" {
  description = "The tenant's webhook signing secret ARNs, keyed github_webhook_secret and jira_webhook_secret, and the tenant key that encrypts them, which the inbound webhook's role needs through Secrets Manager, or null without create_webhook_secrets."
  value = local.webhook_enabled ? {
    secret_arns = { for key, secret in aws_secretsmanager_secret.webhook : key => secret.arn }
    kms_key_arn = local.kms_key_arn
  } : null
}
