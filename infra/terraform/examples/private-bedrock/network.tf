locals {
  az_pool = length(var.availability_zones) > 0 ? var.availability_zones : (
    var.create_network ? data.aws_availability_zones.available[0].names : []
  )

  egress_mode         = coalesce(var.egress_mode, var.create_network ? "nat_instance" : "none")
  create_public_tier  = var.create_network && local.egress_mode != "none"
  create_dns_firewall = coalesce(var.create_dns_firewall, var.create_network)

  app_cidrs = var.create_network ? aws_subnet.private[*].cidr_block : var.endpoint_allowed_cidrs

  management_subnet_ids = var.create_network ? aws_subnet.management[*].id : (
    length(var.management_subnet_ids) > 0 ? var.management_subnet_ids : var.subnet_ids
  )
  management_cidrs = var.create_network ? aws_subnet.management[*].cidr_block : var.management_cidr_blocks

  data_subnet_ids = var.create_network ? aws_subnet.data[*].id : var.data_subnet_ids
  data_cidrs      = var.create_network ? aws_subnet.data[*].cidr_block : []

  data_tier_sources = distinct(concat(local.app_cidrs, local.management_cidrs))
  data_tier_ports   = [5432, 6379, 2049]

  data_nacl_ingress = concat(
    [for pair in setproduct(local.data_tier_sources, local.data_tier_ports) : { cidr = pair[0], from = pair[1], to = pair[1], protocol = "tcp" }],
    [for cidr in local.data_cidrs : { cidr = cidr, from = 0, to = 0, protocol = "-1" }],
  )
  data_nacl_egress = concat(
    [for cidr in local.data_tier_sources : { cidr = cidr, from = 1024, to = 65535, protocol = "tcp" }],
    [for cidr in local.data_cidrs : { cidr = cidr, from = 0, to = 0, protocol = "-1" }],
  )
}

resource "aws_subnet" "management" {
  count = var.create_network ? length(var.management_subnet_cidrs) : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.management_subnet_cidrs[count.index]
  availability_zone       = local.az_pool[count.index % length(local.az_pool)]
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${var.name_prefix}-management-${count.index}", Tier = "management" })
}

resource "aws_route_table" "management" {
  count = var.create_network ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-management", Tier = "management" })
}

resource "aws_route_table_association" "management" {
  count = var.create_network ? length(var.management_subnet_cidrs) : 0

  subnet_id      = aws_subnet.management[count.index].id
  route_table_id = aws_route_table.management[0].id
}

resource "aws_subnet" "data" {
  count = var.create_network ? length(var.data_subnet_cidrs) : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.data_subnet_cidrs[count.index]
  availability_zone       = local.az_pool[count.index % length(local.az_pool)]
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${var.name_prefix}-data-${count.index}", Tier = "data" })
}

resource "aws_route_table" "data" {
  count = var.create_network ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-data", Tier = "data" })
}

resource "aws_route_table_association" "data" {
  count = var.create_network ? length(var.data_subnet_cidrs) : 0

  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data[0].id
}

resource "aws_network_acl" "data" {
  count = var.create_network ? 1 : 0

  vpc_id     = aws_vpc.this[0].id
  subnet_ids = aws_subnet.data[*].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-data", Tier = "data" })
}

resource "aws_network_acl_rule" "data_ingress" {
  count = var.create_network ? length(local.data_nacl_ingress) : 0

  network_acl_id = aws_network_acl.data[0].id
  egress         = false
  rule_number    = 100 + count.index
  rule_action    = "allow"
  protocol       = local.data_nacl_ingress[count.index].protocol
  cidr_block     = local.data_nacl_ingress[count.index].cidr
  from_port      = local.data_nacl_ingress[count.index].from
  to_port        = local.data_nacl_ingress[count.index].to
}

resource "aws_network_acl_rule" "data_egress" {
  count = var.create_network ? length(local.data_nacl_egress) : 0

  network_acl_id = aws_network_acl.data[0].id
  egress         = true
  rule_number    = 100 + count.index
  rule_action    = "allow"
  protocol       = local.data_nacl_egress[count.index].protocol
  cidr_block     = local.data_nacl_egress[count.index].cidr
  from_port      = local.data_nacl_egress[count.index].from
  to_port        = local.data_nacl_egress[count.index].to
}

resource "aws_internet_gateway" "this" {
  count = local.create_public_tier ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count = local.create_public_tier ? length(var.public_subnet_cidrs) : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = local.az_pool[count.index % length(local.az_pool)]
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${var.name_prefix}-public-${count.index}", Tier = "public" })
}

resource "aws_route_table" "public" {
  count = local.create_public_tier ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-public", Tier = "public" })
}

resource "aws_route" "public_default" {
  count = local.create_public_tier ? 1 : 0

  route_table_id         = aws_route_table.public[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this[0].id
}

resource "aws_route_table_association" "public" {
  count = local.create_public_tier ? length(var.public_subnet_cidrs) : 0

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

module "egress" {
  source = "../../egress"

  egress_mode             = local.egress_mode
  name_prefix             = var.name_prefix
  vpc_id                  = local.vpc_id
  public_subnet_id        = local.create_public_tier ? aws_subnet.public[0].id : ""
  private_route_table_ids = var.create_network ? [aws_route_table.private[0].id, aws_route_table.management[0].id] : []
  source_cidr_blocks      = distinct(concat(local.app_cidrs, local.management_cidrs))
  kms_key_arn             = var.kms_key_arn
  tags                    = var.tags

  depends_on = [aws_route.public_default]
}

module "dns_firewall" {
  source = "../../dns-firewall"

  create_dns_firewall        = local.create_dns_firewall
  name_prefix                = var.name_prefix
  vpc_id                     = local.vpc_id
  default_action             = var.dns_firewall_default_action
  additional_allowed_domains = var.dns_firewall_additional_domains
  kms_key_arn                = var.kms_key_arn
  tags                       = var.tags
}
