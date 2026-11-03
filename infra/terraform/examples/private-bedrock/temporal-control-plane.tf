module "temporal_control_plane" {
  source = "../../temporal-control-plane"

  create_temporal_control_plane = var.create_temporal_control_plane
  name_prefix                   = var.name_prefix
  frontend_acm_certificate_arn  = var.temporal_frontend_acm_certificate_arn
  internode_acm_certificate_arn = var.temporal_internode_acm_certificate_arn
  secret_recovery_window_days   = var.secret_recovery_window_days
  tags                          = var.tags
}
