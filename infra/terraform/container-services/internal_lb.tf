resource "terraform_data" "internal_lb_guard" {
  count = local.internal_lb ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.vpc_id != "" && length(var.application_subnet_ids) >= 2 && var.access_log_bucket != ""
      error_message = "The internal load balancer needs vpc_id, application_subnet_ids in two Availability Zones, and access_log_bucket."
    }
  }
}

resource "aws_security_group" "internal_client" {
  count = local.internal_lb ? 1 : 0

  name_prefix = "${var.name_prefix}-internal-client-"
  description = "Workloads that call the internal services. Egress 443 to the internal load balancer."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-internal-client" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "client_to_internal_lb" {
  count = local.internal_lb ? 1 : 0

  security_group_id            = aws_security_group.internal_client[0].id
  description                  = "HTTPS to the internal load balancer"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.internal_lb[0].id
}

resource "aws_security_group" "internal_lb" {
  count = local.internal_lb ? 1 : 0

  name_prefix = "${var.name_prefix}-internal-lb-"
  description = "Internal load balancer. Admits HTTPS from its client group and the allowed networks only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-internal-lb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "internal_lb_from_clients" {
  count = local.internal_lb ? 1 : 0

  security_group_id            = aws_security_group.internal_lb[0].id
  description                  = "HTTPS from the internal client group"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.internal_client[0].id
}

resource "aws_vpc_security_group_ingress_rule" "internal_lb_from_networks" {
  for_each = local.internal_lb ? toset(var.internal_allowed_cidr_blocks) : toset([])

  security_group_id = aws_security_group.internal_lb[0].id
  description       = "HTTPS from an allowed network"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "internal_lb_to_targets" {
  for_each = local.internal_lb_services

  security_group_id            = aws_security_group.internal_lb[0].id
  description                  = "TLS to the ${each.key} tasks"
  from_port                    = each.value.port
  to_port                      = each.value.port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.internal_target[each.key].id
}

resource "aws_security_group" "internal_target" {
  for_each = local.internal_lb_services

  name_prefix = "${var.name_prefix}-${each.value.short}-target-"
  description = "${each.key} tasks. Admits port ${each.value.port} from the internal load balancer only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.value.short}-target" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "target_from_internal_lb" {
  for_each = local.internal_lb_services

  security_group_id            = aws_security_group.internal_target[each.key].id
  description                  = "TLS from the internal load balancer"
  from_port                    = each.value.port
  to_port                      = each.value.port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.internal_lb[0].id
}

resource "aws_lb" "internal" {
  count = local.internal_lb ? 1 : 0

  name                       = substr("${var.name_prefix}-internal", 0, 32)
  internal                   = true
  load_balancer_type         = "application"
  subnets                    = var.application_subnet_ids
  security_groups            = [aws_security_group.internal_lb[0].id]
  drop_invalid_header_fields = true

  access_logs {
    bucket  = aws_s3_bucket.lb_logs[0].id
    prefix  = "internal"
    enabled = true
  }

  tags = var.tags

  depends_on = [aws_s3_bucket_policy.lb_logs]
}

resource "aws_lb_target_group" "internal" {
  for_each = local.internal_lb_services

  name                 = substr("${var.name_prefix}-${each.value.short}", 0, 32)
  port                 = each.value.port
  protocol             = "HTTPS"
  protocol_version     = each.value.protocol_version
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    protocol            = "HTTPS"
    path                = each.value.health_path
    matcher             = each.value.matcher
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
  }

  tags = var.tags
}

resource "aws_lb_listener" "internal" {
  count = local.internal_lb ? 1 : 0

  load_balancer_arn = aws_lb.internal[0].arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.internal_acm_certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "No internal service answers under this name."
      status_code  = "404"
    }
  }

  tags = var.tags
}

resource "aws_lb_listener_rule" "internal" {
  for_each = local.internal_lb_services

  listener_arn = aws_lb_listener.internal[0].arn
  priority     = 10 + 10 * index(sort(keys(local.internal_services)), each.key)

  condition {
    host_header {
      values = ["${each.key}.${var.internal_domain}"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.internal[each.key].arn
  }

  tags = var.tags
}

resource "aws_route53_zone" "internal" {
  count = local.internal_lb ? 1 : 0

  name    = var.internal_domain
  comment = "Names of the platform services behind the internal load balancer."

  vpc {
    vpc_id = var.vpc_id
  }

  tags = var.tags
}

resource "aws_route53_record" "internal" {
  for_each = local.internal_lb_services

  zone_id = aws_route53_zone.internal[0].zone_id
  name    = "${each.key}.${var.internal_domain}"
  type    = "A"

  alias {
    name                   = aws_lb.internal[0].dns_name
    zone_id                = aws_lb.internal[0].zone_id
    evaluate_target_health = false
  }
}
