module "temporal_control_plane" {
  source = "../../temporal-control-plane"

  create_temporal_control_plane = var.create_temporal_control_plane
  name_prefix                   = var.name_prefix
  tags                          = var.tags
}
