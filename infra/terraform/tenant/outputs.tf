output "tenant" {
  description = "What the deployment needs of the tenant, or null when the module is disabled."
  value = local.enabled ? {
    name               = var.tenant
    description        = var.description
    kms_key_arn        = local.kms_key_arn
    secret_arns        = { for name, secret in aws_secretsmanager_secret.this : name => secret.arn }
    worker_policy_arn  = aws_iam_policy.worker[0].arn
    image_prefix       = local.image_prefix
    config_prefix      = local.config_prefix
    publisher_role_arn = try(aws_iam_role.publisher[0].arn, null)
    namespace          = "tenant-${var.tenant}"
    sandbox_namespace  = "tenant-${var.tenant}-sandbox"
    ticket_store = {
      bucket          = module.ticket_store.bucket
      bucket_arn      = module.ticket_store.bucket_arn
      prefix          = module.ticket_store.prefix
      reader_role_arn = module.ticket_store.reader_role_arn
      writer_role_arn = module.ticket_store.writer_role_arn
    }
  } : null
}
