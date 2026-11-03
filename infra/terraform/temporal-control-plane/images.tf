module "server_repository" {
  source = "../ecr"

  create_repository = var.create_temporal_control_plane
  repository_name   = "${var.name_prefix}/temporal-server"
  kms_key_arn       = var.kms_key_arn
  name_prefix       = var.name_prefix
  tags              = var.tags
}

module "admin_tools_repository" {
  source = "../ecr"

  create_repository = var.create_temporal_control_plane
  repository_name   = "${var.name_prefix}/temporal-admin-tools"
  kms_key_arn       = var.kms_key_arn
  name_prefix       = var.name_prefix
  tags              = var.tags
}
