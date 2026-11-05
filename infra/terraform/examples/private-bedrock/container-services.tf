module "container_services" {
  source = "../../container-services"

  create_container_services = var.create_container_services
  name_prefix               = var.name_prefix

  tags = var.tags
}
