locals {
  deploy = local.enabled && var.server_image != "" && var.admin_tools_image != ""
  ui     = local.deploy && var.ui_image != "" && var.ui_acm_certificate_arn != ""

  frontend_port = 7233
  ui_port       = 8080
  # Every Temporal service runs in each server task. Membership gossip uses 6933-6939, and the
  # frontend, history, matching, internal frontend, and worker gRPC ports are 7233-7239.
  internode_port_ranges = [[6933, 6939], [7233, 7239]]
}

resource "terraform_data" "inputs_guard" {
  count = local.deploy ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.cluster_arn != "" && var.vpc_id != "" && length(var.application_subnet_ids) > 0 &&
        length(var.management_subnet_ids) > 0 && var.endpoints_security_group_id != "" &&
        var.database_host != "" && var.database_secret_arn != "" && var.database_secret_kms_key_arn != "" &&
        length(var.database_cidr_blocks) > 0 && var.access_log_bucket != ""
      )
      error_message = "Deploying the server needs cluster_arn, vpc_id, the application and management subnets, endpoints_security_group_id, the database host, CIDRs, secret, and secret key, and access_log_bucket."
    }
    precondition {
      condition     = !local.ui || length(var.management_subnet_ids) >= 2
      error_message = "The UI load balancer needs two management subnets in different Availability Zones."
    }
    precondition {
      condition     = !var.enable_execute_command || var.exec_task_policy_arn != ""
      error_message = "enable_execute_command needs exec_task_policy_arn from the ecs-cluster module."
    }
  }
}

data "aws_ec2_managed_prefix_list" "s3" {
  count = local.deploy ? 1 : 0

  name = "com.amazonaws.${local.region}.s3"
}

resource "aws_security_group" "client" {
  count = local.deploy ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-client-"
  description = "Temporal clients. The frontend NLB admits only this group, so a workload reaches Temporal by attaching it."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-client" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "client_to_frontend" {
  count = local.deploy ? 1 : 0

  security_group_id            = aws_security_group.client[0].id
  description                  = "Temporal frontend gRPC through the NLB"
  from_port                    = local.frontend_port
  to_port                      = local.frontend_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.nlb[0].id
}

resource "aws_security_group" "nlb" {
  count = local.deploy ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-nlb-"
  description = "Temporal frontend NLB. Admits the frontend port from the client group only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-nlb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "nlb_from_clients" {
  count = local.deploy ? 1 : 0

  security_group_id            = aws_security_group.nlb[0].id
  description                  = "Temporal frontend gRPC from the client group"
  from_port                    = local.frontend_port
  to_port                      = local.frontend_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.client[0].id
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_server" {
  count = local.deploy ? 1 : 0

  security_group_id            = aws_security_group.nlb[0].id
  description                  = "Temporal frontend gRPC to the server tasks"
  from_port                    = local.frontend_port
  to_port                      = local.frontend_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.server[0].id
}

resource "aws_security_group" "server" {
  count = local.deploy ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-server-"
  description = "Temporal server tasks. Admits the frontend from the NLB and the internode ports from each other."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-server" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "server_from_nlb" {
  count = local.deploy ? 1 : 0

  security_group_id            = aws_security_group.server[0].id
  description                  = "Temporal frontend gRPC from the NLB"
  from_port                    = local.frontend_port
  to_port                      = local.frontend_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.nlb[0].id
}

resource "aws_vpc_security_group_ingress_rule" "server_internode" {
  for_each = local.deploy ? { for range in local.internode_port_ranges : "${range[0]}-${range[1]}" => range } : {}

  security_group_id            = aws_security_group.server[0].id
  description                  = "Temporal membership and service gRPC between server tasks"
  from_port                    = each.value[0]
  to_port                      = each.value[1]
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.server[0].id
}

resource "aws_vpc_security_group_egress_rule" "server_internode" {
  for_each = local.deploy ? { for range in local.internode_port_ranges : "${range[0]}-${range[1]}" => range } : {}

  security_group_id            = aws_security_group.server[0].id
  description                  = "Temporal membership and service gRPC between server tasks"
  from_port                    = each.value[0]
  to_port                      = each.value[1]
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.server[0].id
}

resource "aws_security_group" "admin_tools" {
  count = local.deploy ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-admin-tools-"
  description = "Temporal schema and namespace tasks. No ingress."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-admin-tools" })

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  # Every task pulls from ECR and reads Secrets Manager through the endpoints, and pulls image
  # layers from S3 through its gateway endpoint.
  task_groups = local.deploy ? merge(
    { server = aws_security_group.server[0].id, admin_tools = aws_security_group.admin_tools[0].id },
    local.ui ? { ui = aws_security_group.ui[0].id } : {},
  ) : {}
  database_groups = local.deploy ? {
    for pair in setproduct(["server", "admin_tools"], var.database_cidr_blocks) :
    "${pair[0]}-${pair[1]}" => { group = local.task_groups[pair[0]], cidr = pair[1] }
  } : {}
}

resource "aws_vpc_security_group_egress_rule" "task_to_endpoints" {
  for_each = local.task_groups

  security_group_id            = each.value
  description                  = "HTTPS to the shared VPC endpoints"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "task_to_s3" {
  for_each = local.task_groups

  security_group_id = each.value
  description       = "HTTPS to S3 for ECR image layers through the gateway endpoint"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3[0].id
}

resource "aws_vpc_security_group_egress_rule" "task_to_database" {
  for_each = local.database_groups

  security_group_id = each.value.group
  description       = "PostgreSQL to Aurora in the data tier"
  from_port         = var.database_port
  to_port           = var.database_port
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value.cidr
}

resource "aws_lb" "frontend" {
  count = local.deploy ? 1 : 0

  name                             = "${var.name_prefix}-temporal"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = var.application_subnet_ids
  security_groups                  = [aws_security_group.nlb[0].id]
  enable_cross_zone_load_balancing = true

  access_logs {
    bucket  = aws_s3_bucket.lb_logs[0].id
    prefix  = "frontend"
    enabled = true
  }

  tags = var.tags

  depends_on = [aws_s3_bucket_policy.lb_logs]
}

resource "aws_lb_target_group" "frontend" {
  count = local.deploy ? 1 : 0

  name                 = "${var.name_prefix}-temporal-${local.frontend_from_secrets ? "tcp" : "tls"}"
  port                 = local.frontend_port
  protocol             = local.frontend_from_secrets ? "TCP" : "TLS"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 10
  }

  tags = var.tags

  # The listener and the server service move to the replacement before this one is deleted.
  lifecycle {
    create_before_destroy = true
  }
}

resource "terraform_data" "frontend_mode" {
  count = local.deploy ? 1 : 0

  input = local.frontend_from_secrets ? "tcp" : "tls"
}

resource "aws_lb_listener" "frontend" {
  count = local.deploy ? 1 : 0

  load_balancer_arn = aws_lb.frontend[0].arn
  port              = local.frontend_port
  protocol          = local.frontend_from_secrets ? "TCP" : "TLS"
  certificate_arn   = local.frontend_from_secrets ? null : var.frontend_acm_certificate_arn
  ssl_policy        = local.frontend_from_secrets ? null : "ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04"
  alpn_policy       = local.frontend_from_secrets ? null : "HTTP2Only"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend[0].arn
  }

  tags = var.tags

  # The provider keeps ssl_policy in a TLS to TCP update, which ELB rejects, so a mode switch replaces the listener.
  lifecycle {
    replace_triggered_by = [terraform_data.frontend_mode]
  }
}
