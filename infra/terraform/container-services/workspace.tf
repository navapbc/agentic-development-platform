locals {
  # uid 10001 is appuser in the platform and repository-intelligence images.
  workspace_access_points = merge(
    {
      checkouts = { path = "/checkouts", permissions = "0755" }
      index     = { path = "/repository-indexes", permissions = "0750" }
    },
    length(var.tenant_workspaces) > 0 ? { tenants = { path = local.tenants_path, permissions = "0755" } } : {},
  )
  tenant_access_points = { for tenant in var.tenant_workspaces : tenant => "/tenants/${tenant}" }
  workspace_access_point_arns = concat(
    [for point in aws_efs_access_point.workspace : point.arn],
    [for point in aws_efs_access_point.tenant : point.arn],
  )
}

resource "terraform_data" "platform_settings_guard" {
  lifecycle {
    precondition {
      condition     = length(local.overridden_settings) == 0
      error_message = "platform_settings may not set a variable the module sets: ${join(", ", local.overridden_settings)}."
    }
  }
}

resource "aws_efs_file_system" "workspace" {
  count = local.services_enabled ? 1 : 0

  encrypted        = true
  kms_key_id       = aws_kms_key.services[0].arn
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  tags = merge(var.tags, { Name = "${var.name_prefix}-workspace" })
}

resource "aws_security_group" "workspace" {
  count = local.services_enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-workspace-"
  description = "Workspace file system mount targets. Admit NFS from the tasks that mount it only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-workspace" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "workspace_from_workloads" {
  for_each = local.mounting_workloads

  security_group_id            = aws_security_group.workspace[0].id
  description                  = "NFS from the ${each.key} tasks"
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.workload[each.key].id
}

resource "aws_efs_mount_target" "workspace" {
  count = local.services_enabled ? length(var.data_subnet_ids) : 0

  file_system_id  = aws_efs_file_system.workspace[0].id
  subnet_id       = var.data_subnet_ids[count.index]
  security_groups = [aws_security_group.workspace[0].id]
}

resource "aws_vpc_security_group_ingress_rule" "workspace_from_clients" {
  count = local.services_enabled ? length(var.workspace_client_security_group_ids) : 0

  security_group_id            = aws_security_group.workspace[0].id
  description                  = "NFS from a client outside the module, such as the EKS nodes"
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.workspace_client_security_group_ids[count.index]
}

resource "aws_efs_access_point" "workspace" {
  for_each = local.services_enabled ? local.workspace_access_points : {}

  file_system_id = aws_efs_file_system.workspace[0].id

  posix_user {
    uid = 10001
    gid = 10001
  }

  root_directory {
    path = each.value.path
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = each.value.permissions
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-workspace-${each.key}" })
}

# Each tenant's checkouts live under their own root, outside /checkouts, so no other tenant's
# access point reaches them.
resource "aws_efs_access_point" "tenant" {
  for_each = local.services_enabled ? local.tenant_access_points : {}

  file_system_id = aws_efs_file_system.workspace[0].id

  posix_user {
    uid = 10001
    gid = 10001
  }

  root_directory {
    path = each.value
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0750"
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-workspace-tenant-${each.key}", tenant = each.key })
}

resource "aws_efs_file_system_policy" "workspace" {
  count = local.services_enabled ? 1 : 0

  file_system_id = aws_efs_file_system.workspace[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTls"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "*"
        Resource  = aws_efs_file_system.workspace[0].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyOutsideTheAccessPoints"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite", "elasticfilesystem:ClientRootAccess"]
        Resource  = aws_efs_file_system.workspace[0].arn
        Condition = { StringNotEquals = { "elasticfilesystem:AccessPointArn" = local.workspace_access_point_arns } }
      },
    ]
  })
}
