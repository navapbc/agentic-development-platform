locals {
  use_instance = var.egress_mode == "nat_instance"
  use_gateway  = var.egress_mode == "nat_gateway"
  enabled      = local.use_instance || local.use_gateway
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = !local.enabled || (var.public_subnet_id != "" && var.vpc_id != "")
      error_message = "egress_mode nat_instance and nat_gateway require vpc_id and public_subnet_id."
    }
    precondition {
      condition     = !local.use_instance || length(var.source_cidr_blocks) > 0
      error_message = "egress_mode nat_instance requires source_cidr_blocks."
    }
  }
}

resource "aws_eip" "nat" {
  count = local.enabled ? 1 : 0

  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name_prefix}-nat" })
}

resource "aws_nat_gateway" "this" {
  count = local.use_gateway ? 1 : 0

  subnet_id     = var.public_subnet_id
  allocation_id = aws_eip.nat[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })
}

data "aws_ssm_parameter" "nat_ami" {
  count = local.use_instance ? 1 : 0

  name = var.nat_instance_ami_ssm_parameter
}

resource "aws_security_group" "nat_instance" {
  count = local.use_instance ? 1 : 0

  name_prefix = "${var.name_prefix}-nat-"
  description = "NAT instance. Forwards the allowed ports from the private tiers only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "nat_from_private" {
  for_each = local.use_instance ? { for pair in setproduct(var.source_cidr_blocks, var.allowed_egress_ports) : "${pair[0]}-${pair[1]}" => pair } : {}

  security_group_id = aws_security_group.nat_instance[0].id
  cidr_ipv4         = each.value[0]
  ip_protocol       = "tcp"
  from_port         = each.value[1]
  to_port           = each.value[1]
  description       = "Forwarded traffic from a private tier"
}

resource "aws_vpc_security_group_egress_rule" "nat_to_internet" {
  for_each = local.use_instance ? toset([for port in var.allowed_egress_ports : tostring(port)]) : toset([])

  security_group_id = aws_security_group.nat_instance[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  description       = "Forwarded traffic and instance updates to the internet"
}

data "aws_iam_policy_document" "nat_assume" {
  count = local.use_instance ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "nat_instance" {
  count = local.use_instance ? 1 : 0

  name_prefix        = "${var.name_prefix}-nat-"
  assume_role_policy = data.aws_iam_policy_document.nat_assume[0].json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "nat_ssm" {
  count = local.use_instance ? 1 : 0

  role       = aws_iam_role.nat_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "nat_instance" {
  count = local.use_instance ? 1 : 0

  name_prefix = "${var.name_prefix}-nat-"
  role        = aws_iam_role.nat_instance[0].name
  tags        = var.tags
}

resource "aws_instance" "nat" {
  count = local.use_instance ? 1 : 0

  ami                    = data.aws_ssm_parameter.nat_ami[0].value
  instance_type          = var.nat_instance_type
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [aws_security_group.nat_instance[0].id]
  iam_instance_profile   = aws_iam_instance_profile.nat_instance[0].name
  source_dest_check      = false

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/nat-instance-user-data.sh.tftpl", {
    source_cidr_blocks = var.source_cidr_blocks
  })

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  maintenance_options {
    auto_recovery = "default"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
    kms_key_id  = var.kms_key_arn == "" ? null : var.kms_key_arn
  }

  tags        = merge(var.tags, { Name = "${var.name_prefix}-nat" })
  volume_tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_eip_association" "nat_instance" {
  count = local.use_instance ? 1 : 0

  instance_id   = aws_instance.nat[0].id
  allocation_id = aws_eip.nat[0].id
}

resource "aws_route" "default_via_instance" {
  count = local.use_instance ? length(var.private_route_table_ids) : 0

  route_table_id         = var.private_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_instance.nat[0].primary_network_interface_id
}

resource "aws_route" "default_via_gateway" {
  count = local.use_gateway ? length(var.private_route_table_ids) : 0

  route_table_id         = var.private_route_table_ids[count.index]
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}
