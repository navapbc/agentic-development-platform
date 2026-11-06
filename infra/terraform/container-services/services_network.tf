data "aws_ec2_managed_prefix_list" "s3" {
  count = local.services_enabled ? 1 : 0

  name = "com.amazonaws.${local.region}.s3"
}

locals {
  mounting_workloads = { for name, spec in local.workloads : name => spec if length(spec.mounts) > 0 }
  database_egress = {
    for pair in setproduct([for name, spec in local.workloads : name if spec.database], var.database_cidr_blocks) :
    "${pair[0]}-${pair[1]}" => { workload = pair[0], cidr = pair[1] }
  }

  workload_security_groups = {
    for name, spec in local.all_workloads : name => concat(
      [aws_security_group.workload[name].id],
      spec.temporal ? [var.temporal.client_security_group_id] : [],
      spec.internal_client ? [aws_security_group.internal_client[0].id] : [],
      spec.target != "" ? [aws_security_group.internal_target[spec.target].id] : [],
    )
  }
}

resource "aws_security_group" "workload" {
  for_each = local.all_workloads

  name_prefix = "${var.name_prefix}-${local.role_labels[each.key]}-"
  description = each.key == "clickhouse" ? "ClickHouse task. Ingress over TLS from the Langfuse tasks only. Egress to the endpoints, S3, and its file system." : "${each.key} tasks. No ingress of its own. Egress to the endpoints, S3, and what the task declares."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.key}" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "workload_to_endpoints" {
  for_each = local.all_workloads

  security_group_id            = aws_security_group.workload[each.key].id
  description                  = "HTTPS to the shared VPC endpoints"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "workload_to_s3" {
  for_each = local.all_workloads

  security_group_id = aws_security_group.workload[each.key].id
  description       = "HTTPS to S3 through the gateway endpoint, for image layers"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3[0].id
}

resource "aws_vpc_security_group_egress_rule" "workload_to_database" {
  for_each = local.database_egress

  security_group_id = aws_security_group.workload[each.value.workload].id
  description       = "PostgreSQL to the data tier"
  from_port         = var.database_port
  to_port           = var.database_port
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value.cidr
}

resource "aws_vpc_security_group_egress_rule" "workload_to_workspace" {
  for_each = local.mounting_workloads

  security_group_id            = aws_security_group.workload[each.key].id
  description                  = "NFS over TLS to the workspace file system"
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.workspace[0].id
}
