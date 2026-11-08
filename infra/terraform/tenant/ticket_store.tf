module "ticket_store" {
  source = "../rebar-ticket-store"

  create_rebar_ticket_store = local.enabled
  name_prefix               = local.name
  bucket_prefix             = "${var.tenant}-rebar-tickets-"
  access_log_bucket         = var.access_log_bucket
  trusted_principals        = concat([var.worker_role_arn], var.ticket_store.operators)
  admin_principals          = var.ticket_store.admins
  writer_github_oidc_trust  = var.ticket_store.writer_github_oidc_trust
  tags                      = local.tags
}
