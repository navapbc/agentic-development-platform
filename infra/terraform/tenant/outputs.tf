output "tenant" {
  description = "What the deployment needs of the tenant, or null when the module is disabled."
  value = local.enabled ? {
    name        = var.tenant
    description = var.description
    kms_key_arn = local.kms_key_arn
    secret_arns = merge(
      { for name, secret in aws_secretsmanager_secret.this : name => secret.arn },
      { repo_intelligence_token = local.repo_intelligence_token_arn },
      { mcp_gateway_token = local.mcp_gateway_token_arn },
      { escalation_action_key = local.escalation_action_key_arn },
    )
    repo_intelligence_token_arn = local.repo_intelligence_token_arn
    mcp_gateway_token_arn       = local.mcp_gateway_token_arn
    action_key_fingerprint_arn  = local.escalation_action_key_sha256_arn
    worker_policy_arn           = aws_iam_policy.worker[0].arn
    review_policy_arn           = aws_iam_policy.review[0].arn
    review_signing_key_arn      = local.review_signing_key_arn
    image_prefix                = local.image_prefix
    config_prefix               = local.config_prefix
    publisher_role_arn          = try(aws_iam_role.publisher[0].arn, null)
    claims_probe_role_arn       = try(aws_iam_role.claims_probe[0].arn, null)
    signing_profile             = aws_signer_signing_profile.this[0].arn
    namespace                   = "tenant-${var.tenant}"
    sandbox_namespace           = "tenant-${var.tenant}-sandbox"
    review_namespace            = "tenant-${var.tenant}-review"
    ticket_store = {
      bucket          = module.ticket_store.bucket
      bucket_arn      = module.ticket_store.bucket_arn
      prefix          = module.ticket_store.prefix
      reader_role_arn = module.ticket_store.reader_role_arn
      writer_role_arn = module.ticket_store.writer_role_arn
    }
  } : null
}
