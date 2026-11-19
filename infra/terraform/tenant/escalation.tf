locals {
  escalation_action_key_arn     = try(aws_secretsmanager_secret.escalation_action_key[0].arn, "")
  escalation_action_key_readers = concat([var.worker_role_arn], local.webhook_enabled || local.slack_webhook_enabled ? [var.webhook_reader_arn] : [])
}

resource "aws_secretsmanager_secret" "escalation_action_key" {
  count = local.enabled ? 1 : 0

  name                    = "${local.secret_prefix}escalation-action-key"
  description             = "Key that signs and verifies the action tokens of the ${var.tenant} tenant's escalation messages. Its activity worker signs with it, and the platform's inbound webhook verifies the tenant's Slack actions with it."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

ephemeral "random_password" "escalation_action_key" {
  count = local.enabled ? 1 : 0

  length  = 48
  special = false
}

resource "aws_secretsmanager_secret_version" "escalation_action_key" {
  count = local.enabled ? 1 : 0

  secret_id                = aws_secretsmanager_secret.escalation_action_key[0].id
  secret_string_wo         = ephemeral.random_password.escalation_action_key[0].result
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret_policy" "escalation_action_key" {
  count = local.enabled ? 1 : 0

  secret_arn          = aws_secretsmanager_secret.escalation_action_key[0].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheTenantWorkerAndTheInboundWebhook"
        Effect    = "Allow"
        Principal = { AWS = local.escalation_action_key_readers }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.escalation_action_key_readers } }
      },
    ]
  })
}
