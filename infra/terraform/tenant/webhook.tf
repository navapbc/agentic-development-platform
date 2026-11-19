variable "create_webhook_secrets" {
  description = "Create the tenant's GitHub and Jira signing secrets, which only webhook_reader_arn reads. The platform's inbound webhook verifies the tenant's GitHub and Jira deliveries with them when it serves more than one tenant, and the deployment grants its role every tenant's secrets, keys, and bundles in one policy."
  type        = bool
  default     = false
}

variable "create_slack_webhook_secret" {
  description = "Create the tenant's Slack signing secret, which only webhook_reader_arn reads, and admit webhook_reader_arn to the tenant's escalation action key, whatever the tenant count. The inbound webhook verifies the tenant's Slack actions with both, because the tenant's worker signs them with the action key."
  type        = bool
  default     = false
}

variable "webhook_reader_arn" {
  description = "Pod Identity role ARN of the platform's inbound webhook, the one principal that reads the tenant's webhook signing secrets and, beside the tenant worker, its escalation action key. Required with create_webhook_secrets or create_slack_webhook_secret."
  type        = string
  default     = ""
}

locals {
  webhook_enabled       = local.enabled && var.create_webhook_secrets
  slack_webhook_enabled = local.enabled && var.create_slack_webhook_secret
  webhook_secret_keys   = ["github_webhook_secret", "jira_webhook_secret", "slack_signing_secret"]
  webhook_secret_names = toset(concat(
    local.webhook_enabled ? ["github_webhook_secret", "jira_webhook_secret"] : [],
    local.slack_webhook_enabled ? ["slack_signing_secret"] : [],
  ))
}

resource "terraform_data" "webhook_guard" {
  count = local.webhook_enabled || local.slack_webhook_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.webhook_reader_arn != ""
      error_message = "create_webhook_secrets and create_slack_webhook_secret need webhook_reader_arn, the Pod Identity role of the platform's inbound webhook."
    }
    precondition {
      condition     = length(setintersection(toset(var.secrets), toset(local.webhook_secret_keys))) == 0
      error_message = "secrets may not name github_webhook_secret, jira_webhook_secret, or slack_signing_secret, which the module creates for the inbound webhook alone."
    }
  }
}

# Each secret holds the value the tenant's GitHub webhook, Jira webhook, or Slack app signs with.
# An operator stores it, and until then the deployment mounts no value and the webhook refuses the
# tenant's deliveries.
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
  description = "The ARNs of the tenant secrets the inbound webhook reads, keyed github_webhook_secret, jira_webhook_secret, slack_signing_secret, and escalation_action_key, and the tenant key that encrypts them, which the inbound webhook's role needs through Secrets Manager, or null without create_webhook_secrets and create_slack_webhook_secret. With create_slack_webhook_secret alone it names only the Slack signing secret and the escalation action key."
  value = local.webhook_enabled || local.slack_webhook_enabled ? {
    secret_arns = merge(
      { for key, secret in aws_secretsmanager_secret.webhook : key => secret.arn },
      { escalation_action_key = local.escalation_action_key_arn },
    )
    kms_key_arn = local.kms_key_arn
  } : null
}
