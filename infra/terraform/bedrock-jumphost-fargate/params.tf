resource "aws_ssm_parameter" "cluster_name" {
  count = local.enabled ? 1 : 0

  name  = "${local.param_prefix}/cluster_name"
  type  = "String"
  value = aws_ecs_cluster.this[0].name
  tags  = var.tags
}

resource "aws_ssm_parameter" "task_definition" {
  count = local.enabled ? 1 : 0

  name  = "${local.param_prefix}/task_definition"
  type  = "String"
  value = aws_ecs_task_definition.this[0].family
  tags  = var.tags
}

resource "aws_ssm_parameter" "region" {
  count = local.enabled ? 1 : 0

  name  = "${local.param_prefix}/region"
  type  = "String"
  value = var.region
  tags  = var.tags
}

resource "aws_ssm_parameter" "aws_vpc_configuration" {
  count = local.enabled ? 1 : 0

  name  = "${local.param_prefix}/aws_vpc_configuration"
  type  = "String"
  value = "{subnets=[${join(",", var.subnet_ids)}],securityGroups=[${aws_security_group.task[0].id}],assignPublicIp=DISABLED}"
  tags  = var.tags
}
