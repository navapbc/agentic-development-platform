data "aws_availability_zones" "available" {
  count = var.create_network ? 1 : 0
  state = "available"
}

locals {
  azs = length(var.availability_zones) > 0 ? var.availability_zones : (
    var.create_network ? slice(data.aws_availability_zones.available[0].names, 0, length(var.new_subnet_cidrs)) : []
  )

  vpc_id     = var.create_network ? aws_vpc.this[0].id : var.vpc_id
  subnet_ids = var.create_network ? aws_subnet.private[*].id : var.subnet_ids

  endpoint_allowed_cidrs = var.create_network ? aws_subnet.private[*].cidr_block : var.endpoint_allowed_cidrs
  route_table_ids        = var.create_network ? [aws_route_table.private[0].id] : var.route_table_ids

  jumphost_image_uri = "${local.ecr_repository_url}:${var.jumphost_image_tag}"

  ecr_repository_url          = var.create_ecr_repository ? module.jumphost_ecr.repository_url : var.existing_ecr_repository_url
  ecr_repository_arn          = var.create_ecr_repository ? module.jumphost_ecr.repository_arn : var.existing_ecr_repository_arn
  endpoints_security_group_id = var.create_endpoints ? module.vpc_endpoints.security_group_id : var.existing_endpoints_security_group_id
  attach_endpoints_egress     = var.create_endpoints || var.existing_endpoints_security_group_id != ""
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = var.create_network || (var.vpc_id != "" && length(var.subnet_ids) > 0 && length(var.endpoint_allowed_cidrs) > 0)
      error_message = "When create_network is false, set vpc_id, subnet_ids, and endpoint_allowed_cidrs."
    }
  }
}

resource "aws_vpc" "this" {
  count = var.create_network ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_subnet" "private" {
  count = var.create_network ? length(var.new_subnet_cidrs) : 0

  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = var.new_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${var.name_prefix}-private-${count.index}" })
}

resource "aws_route_table" "private" {
  count = var.create_network ? 1 : 0

  vpc_id = aws_vpc.this[0].id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-private" })
}

resource "aws_route_table_association" "private" {
  count = var.create_network ? length(var.new_subnet_cidrs) : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

module "cloudtrail" {
  source = "../../cloudtrail"

  create_cloudtrail = var.create_cloudtrail
  trail_name        = var.cloudtrail_name
  region            = var.region
  name_prefix       = var.name_prefix
  tags              = var.tags
}

module "vpc_endpoints" {
  source = "../../vpc-endpoints"

  create_endpoints    = var.create_endpoints
  region              = var.region
  vpc_id              = local.vpc_id
  subnet_ids          = local.subnet_ids
  allowed_cidr_blocks = local.endpoint_allowed_cidrs
  route_table_ids     = local.route_table_ids

  interface_services = [
    "bedrock-runtime",
    "ssmmessages",
    "ecr.api",
    "ecr.dkr",
    "logs",
    "kms",
  ]

  name_prefix = var.name_prefix
  tags        = var.tags
}

module "bedrock_iam" {
  source = "../../bedrock-iam"

  create_invoke_policy = true
  name_prefix          = var.name_prefix
  tags                 = var.tags
}

module "jumphost_ecr" {
  source = "../../ecr"

  create_repository = var.create_ecr_repository
  repository_name   = var.jumphost_repository_name
  name_prefix       = var.name_prefix
  tags              = var.tags
}

module "bedrock_jumphost" {
  source = "../../bedrock-jumphost-fargate"

  create_jumphost             = true
  region                      = var.region
  vpc_id                      = local.vpc_id
  subnet_ids                  = local.subnet_ids
  endpoints_security_group_id = local.endpoints_security_group_id
  attach_endpoints_egress     = local.attach_endpoints_egress
  image_uri                   = local.jumphost_image_uri
  ecr_repository_arn          = local.ecr_repository_arn
  jumphost_name               = var.jumphost_name
  duration_in_secs            = var.jumphost_session_duration_seconds
  name_prefix                 = var.name_prefix
  tags                        = var.tags
}
