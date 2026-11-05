resource "aws_security_group" "ui" {
  count = local.ui ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-ui-"
  description = "Temporal UI tasks. Admits the UI port from its load balancer only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-ui" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "ui_from_alb" {
  count = local.ui ? 1 : 0

  security_group_id            = aws_security_group.ui[0].id
  description                  = "HTTPS from the UI load balancer"
  from_port                    = local.ui_port
  to_port                      = local.ui_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.alb[0].id
}

resource "aws_security_group" "alb" {
  count = local.ui ? 1 : 0

  name_prefix = "${var.name_prefix}-temporal-ui-alb-"
  description = "Temporal UI load balancer. Admits HTTPS from the allowed operator networks and security groups only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal-ui-alb" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_operators" {
  for_each = local.ui ? toset(var.ui_allowed_cidr_blocks) : toset([])

  security_group_id = aws_security_group.alb[0].id
  description       = "HTTPS from an allowed operator network"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_operator_groups" {
  count = local.ui ? length(var.ui_operator_security_group_ids) : 0

  security_group_id            = aws_security_group.alb[0].id
  description                  = "HTTPS from an operator security group"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.ui_operator_security_group_ids[count.index]
}

resource "aws_vpc_security_group_egress_rule" "operator_groups_to_alb" {
  count = local.ui ? length(var.ui_operator_security_group_ids) : 0

  security_group_id            = var.ui_operator_security_group_ids[count.index]
  description                  = "HTTPS to the Temporal UI load balancer"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.alb[0].id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_ui" {
  count = local.ui ? 1 : 0

  security_group_id            = aws_security_group.alb[0].id
  description                  = "HTTPS to the UI tasks"
  from_port                    = local.ui_port
  to_port                      = local.ui_port
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.ui[0].id
}

resource "aws_lb" "ui" {
  count = local.ui ? 1 : 0

  name                       = "${var.name_prefix}-temporal-ui"
  internal                   = true
  load_balancer_type         = "application"
  subnets                    = var.management_subnet_ids
  security_groups            = [aws_security_group.alb[0].id]
  drop_invalid_header_fields = true

  access_logs {
    bucket  = aws_s3_bucket.lb_logs[0].id
    prefix  = "ui"
    enabled = true
  }

  tags = var.tags

  depends_on = [aws_s3_bucket_policy.lb_logs]
}

resource "aws_lb_target_group" "ui" {
  count = local.ui ? 1 : 0

  name                 = "${var.name_prefix}-temporal-ui"
  port                 = local.ui_port
  protocol             = "HTTPS"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    protocol = "HTTPS"
    path     = "/api/v1/settings"
    matcher  = "200"
  }

  tags = var.tags
}

resource "aws_lb_listener" "ui" {
  count = local.ui ? 1 : 0

  load_balancer_arn = aws_lb.ui[0].arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.ui_acm_certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui[0].arn
  }

  tags = var.tags
}

resource "aws_ecs_task_definition" "ui" {
  count = local.ui ? 1 : 0

  # The service keeps running the revision it was last deployed to until inv temporal-deploy
  # moves it, so a superseded revision stays active.
  skip_destroy = true

  family                   = "${var.name_prefix}-temporal-ui"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution["ui"].arn
  task_role_arn            = try(aws_iam_role.task["ui"].arn, null)

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = local.runtime_volume
  }

  container_definitions = jsonencode([
    merge(local.volume_permissions["ui"], { logConfiguration = local.log_settings["ui"] }),
    {
      name                   = "ui"
      dependsOn              = local.after_volume_permissions
      image                  = var.ui_image
      essential              = true
      readonlyRootFilesystem = true
      portMappings           = [{ containerPort = local.ui_port, protocol = "tcp" }]
      environment            = local.ui_environment
      secrets                = local.client_secrets
      mountPoints            = [{ sourceVolume = local.runtime_volume, containerPath = "/tmp" }]
      logConfiguration       = local.log_settings["ui"]
    },
  ])

  tags = var.tags
}

resource "aws_ecs_service" "ui" {
  count = local.ui ? 1 : 0

  name                              = "${var.name_prefix}-temporal-ui"
  cluster                           = var.cluster_arn
  task_definition                   = aws_ecs_task_definition.ui[0].arn
  desired_count                     = 1
  enable_execute_command            = var.enable_execute_command
  health_check_grace_period_seconds = 60
  propagate_tags                    = "SERVICE"

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets         = var.management_subnet_ids
    security_groups = [aws_security_group.ui[0].id, aws_security_group.client[0].id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ui[0].arn
    container_name   = "ui"
    container_port   = local.ui_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = var.tags

  # The off-hours schedule scales the UI with the server.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.ui]
}
