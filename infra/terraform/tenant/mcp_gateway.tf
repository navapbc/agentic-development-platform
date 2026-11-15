locals {
  mcp_gateway_token_arn = try(aws_secretsmanager_secret.mcp_gateway_token[0].arn, "")
}

resource "aws_secretsmanager_secret" "mcp_gateway_token" {
  count = local.enabled ? 1 : 0

  name                    = "${local.secret_prefix}mcp-gateway-token"
  description             = "Bearer token the ${var.tenant} tenant's MCP gateway accepts on /mcp. Its operators and IDE agents present it, and no other tenant's gateway accepts it."
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = local.tags
}

ephemeral "random_password" "mcp_gateway_token" {
  count = local.enabled ? 1 : 0

  length  = 48
  special = false
}

resource "aws_secretsmanager_secret_version" "mcp_gateway_token" {
  count = local.enabled ? 1 : 0

  secret_id                = aws_secretsmanager_secret.mcp_gateway_token[0].id
  secret_string_wo         = ephemeral.random_password.mcp_gateway_token[0].result
  secret_string_wo_version = 1
}

locals {
  mcp_gateway_token_readers = distinct(concat([var.worker_role_arn], var.mcp_gateway_token_reader_arns))
}

# The gateway runs as the tenant's worker identity, which with the named operators is the only
# reader the token admits.
resource "aws_secretsmanager_secret_policy" "mcp_gateway_token" {
  count = local.enabled ? 1 : 0

  secret_arn          = aws_secretsmanager_secret.mcp_gateway_token[0].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheTenantGatewayAndItsOperators"
        Effect    = "Allow"
        Principal = { AWS = local.mcp_gateway_token_readers }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.mcp_gateway_token_readers } }
      },
    ]
  })
}
