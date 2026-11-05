locals {
  enabled = var.create_storage
  name    = "${var.name_prefix}-clickhouse"
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.enabled || (var.vpc_id != "" && length(var.subnet_ids) > 0)
      error_message = "create_storage requires vpc_id and subnet_ids."
    }
    precondition {
      condition     = !local.enabled || length(var.client_security_group_ids) + length(var.client_cidr_blocks) > 0
      error_message = "create_storage requires client_security_group_ids or client_cidr_blocks."
    }
  }
}

resource "aws_efs_file_system" "this" {
  count = local.enabled ? 1 : 0

  creation_token   = local.name
  encrypted        = true
  kms_key_id       = var.kms_key_arn == "" ? null : var.kms_key_arn
  performance_mode = "generalPurpose"
  throughput_mode  = var.throughput_mode

  tags = merge(var.tags, { Name = local.name })
}

resource "aws_efs_backup_policy" "this" {
  count = local.enabled ? 1 : 0

  file_system_id = aws_efs_file_system.this[0].id
  backup_policy {
    status = "ENABLED"
  }
}

resource "aws_security_group" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = "${local.name}-efs-"
  description = "ClickHouse EFS mount targets. Admits NFS from the ClickHouse task. No egress, because the data tier never originates connections."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${local.name}-efs" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "from_security_group" {
  count = local.enabled ? length(var.client_security_group_ids) : 0

  security_group_id            = aws_security_group.this[0].id
  referenced_security_group_id = var.client_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  description                  = "NFS from the ClickHouse task"
}

resource "aws_vpc_security_group_ingress_rule" "from_cidr" {
  for_each = local.enabled ? toset(var.client_cidr_blocks) : toset([])

  security_group_id = aws_security_group.this[0].id
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 2049
  to_port           = 2049
  description       = "NFS from the application tier"
}

resource "aws_efs_mount_target" "this" {
  count = local.enabled ? length(var.subnet_ids) : 0

  file_system_id  = aws_efs_file_system.this[0].id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [aws_security_group.this[0].id]
}

resource "aws_efs_access_point" "clickhouse" {
  count = local.enabled ? 1 : 0

  file_system_id = aws_efs_file_system.this[0].id

  posix_user {
    uid = var.posix_uid
    gid = var.posix_gid
  }

  root_directory {
    path = "/clickhouse"
    creation_info {
      owner_uid   = var.posix_uid
      owner_gid   = var.posix_gid
      permissions = "0750"
    }
  }

  tags = merge(var.tags, { Name = local.name })
}

data "aws_iam_policy_document" "file_system" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "DenyNonTls"
    effect    = "Deny"
    actions   = ["*"]
    resources = [aws_efs_file_system.this[0].arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyOutsideAccessPoint"
    effect    = "Deny"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite", "elasticfilesystem:ClientRootAccess"]
    resources = [aws_efs_file_system.this[0].arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.clickhouse[0].arn]
    }
  }
}

resource "aws_efs_file_system_policy" "this" {
  count = local.enabled ? 1 : 0

  file_system_id = aws_efs_file_system.this[0].id
  policy         = data.aws_iam_policy_document.file_system[0].json
}
