resource "terraform_data" "managed_grafana_guard" {
  count = var.create_managed_grafana ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.container_telemetry_exports
      error_message = "create_managed_grafana needs the collector exports, which create the Prometheus workspace. Set create_container_services, container_collector_image, and container_collector_exports_enabled."
    }
    precondition {
      condition     = !var.grafana_private_access || var.create_endpoints
      error_message = "grafana_private_access needs create_endpoints, which creates the grafana-workspace interface endpoint."
    }
  }
}

module "managed_grafana" {
  source = "../../managed-grafana"

  create_managed_grafana      = var.create_managed_grafana
  name_prefix                 = var.name_prefix
  prometheus_workspace_arn    = try(module.container_services.telemetry.prometheus_workspace_arn, null)
  prometheus_endpoint         = try(module.container_services.telemetry.prometheus_endpoint, null)
  xray_kms_key_arn            = try(module.container_services.telemetry.xray_encryption_key_arn, null)
  operator_principal_arns     = var.grafana_query_principal_arns
  vpc_id                      = local.vpc_id
  subnet_ids                  = local.subnet_ids
  endpoints_security_group_id = local.endpoints_security_group_id
  allowed_vpc_endpoint_ids    = var.grafana_private_access ? compact([lookup(module.vpc_endpoints.interface_endpoint_ids, "grafana-workspace", "")]) : []
  admin_group_ids             = var.grafana_admin_group_ids
  editor_group_ids            = var.grafana_editor_group_ids
  viewer_group_ids            = var.grafana_viewer_group_ids
  tags                        = var.tags
}
