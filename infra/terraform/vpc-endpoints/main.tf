locals {
  enabled     = var.create_endpoints
  s3_gateway  = var.create_endpoints && var.create_s3_gateway
  discover_rt = local.s3_gateway && length(var.route_table_ids) == 0
}

data "aws_route_table" "selected" {
  for_each = local.discover_rt ? toset(var.subnet_ids) : toset([])

  subnet_id = each.value
}

locals {
  s3_route_table_ids = length(var.route_table_ids) > 0 ? var.route_table_ids : distinct([for rt in data.aws_route_table.selected : rt.id])
}

resource "aws_security_group" "endpoints" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-vpce-"
  description = "Ingress to the shared VPC interface endpoints (443 from in-VPC subnets only)."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from allowed in-VPC subnets"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
  }

  egress = []

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpce" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.enabled ? toset(var.interface_services) : toset([])

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = var.private_dns_enabled

  tags = merge(var.tags, { Name = "${var.name_prefix}-${replace(each.value, ".", "-")}" })
}

resource "aws_vpc_endpoint" "s3" {
  count = local.s3_gateway ? 1 : 0

  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.s3_route_table_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-s3" })
}
