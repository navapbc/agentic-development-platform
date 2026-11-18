module "ticket_store" {
  source = "../rebar-ticket-store"

  create_rebar_ticket_store       = local.enabled
  name_prefix                     = local.name
  bucket_name                     = var.ticket_store.bucket_name
  bucket_prefix                   = "${var.name_prefix}-${var.tenant}-rebar-tickets-"
  access_log_bucket               = var.access_log_bucket
  trusted_principals              = var.ticket_store.operators
  trusted_principals_by_condition = local.enabled && var.worker_role_name != "" ? [local.worker_role_principal_arn] : []
  admin_principals                = var.ticket_store.admins
  writer_github_oidc_trust        = var.ticket_store.writer_github_oidc_trust
  tags                            = local.tags
}
