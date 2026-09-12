resource "aws_security_group" "task" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-jumphost-task-"
  description = "Fargate jumphost task. No ingress. Egress 443 to the shared VPC endpoints only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-jumphost-task" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "task_to_endpoints" {
  count = local.enabled && var.attach_endpoints_egress ? 1 : 0

  security_group_id            = aws_security_group.task[0].id
  description                  = "HTTPS to the shared VPC endpoints (Bedrock, SSM, ECR, logs, KMS)"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

data "aws_ec2_managed_prefix_list" "s3" {
  count = local.enabled && var.attach_s3_egress ? 1 : 0

  name = "com.amazonaws.${var.region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "task_to_s3" {
  count = local.enabled && var.attach_s3_egress ? 1 : 0

  security_group_id = aws_security_group.task[0].id
  description       = "HTTPS to S3 (ECR image layers) via the S3 gateway endpoint"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3[0].id
}

resource "aws_vpc_security_group_egress_rule" "task_to_cidrs" {
  for_each = local.enabled ? toset(var.additional_egress_cidrs) : toset([])

  security_group_id = aws_security_group.task[0].id
  description       = "HTTPS to caller-supplied CIDR blocks"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  cidr_ipv4         = each.value
}
