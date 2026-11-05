locals {
  # DNS Firewall already resolves *.internal. Any other internal domain is added to its allowlist.
  container_internal_domains = (
    var.create_container_services && var.container_internal_acm_certificate_arn != "" && !endswith(var.container_internal_domain, ".internal")
    ? ["*.${var.container_internal_domain}"] : []
  )
}

module "container_services" {
  source = "../../container-services"

  create_container_services = var.create_container_services
  name_prefix               = var.name_prefix

  vpc_id                       = local.vpc_id
  application_subnet_ids       = local.subnet_ids
  internal_acm_certificate_arn = var.container_internal_acm_certificate_arn
  internal_domain              = var.container_internal_domain
  internal_allowed_cidr_blocks = var.container_internal_allowed_cidr_blocks
  access_log_bucket            = var.access_log_bucket

  tags = var.tags
}
