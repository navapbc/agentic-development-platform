data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  enabled    = var.create_managed_grafana
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  # Amazon Managed Grafana service endpoints, from the AWS General Reference, without ap-northeast-2,
  # where the service cannot connect a workspace to a private VPC, which this module always does.
  supported_regions = [
    "us-east-1", "us-east-2", "us-west-2",
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
    "eu-central-1", "eu-west-1", "eu-west-2",
    "us-gov-east-1", "us-gov-west-1",
  ]
  # AWS GovCloud (US) offers Amazon Managed Grafana no customer managed key.
  customer_managed_key = local.enabled && local.partition == "aws"

  query_role = local.enabled && length(var.operator_principal_arns) > 0

  role_groups = {
    for role, group_ids in { ADMIN = var.admin_group_ids, EDITOR = var.editor_group_ids, VIEWER = var.viewer_group_ids } :
    role => group_ids if local.enabled && length(group_ids) > 0
  }

  prometheus_query_actions = ["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]
  xray_read_actions = [
    "xray:BatchGetTraces",
    "xray:GetTraceSummaries",
    "xray:GetTraceGraph",
    "xray:GetServiceGraph",
    "xray:GetGroups",
    "xray:GetTimeSeriesServiceStatistics",
    "xray:GetInsightSummaries",
    "xray:GetInsight",
  ]

  workspace_statements = [
    for statement in [
      {
        Sid      = "QueryThePrometheusWorkspace"
        Effect   = "Allow"
        Action   = local.prometheus_query_actions
        Resource = var.prometheus_workspace_arn
        Include  = true
      },
      {
        Sid      = "ReadTraces"
        Effect   = "Allow"
        Action   = local.xray_read_actions
        Resource = "*"
        Include  = true
      },
      {
        Sid      = "ListRegionsForTheTraceDataSource"
        Effect   = "Allow"
        Action   = "ec2:DescribeRegions"
        Resource = "*"
        Include  = true
      },
      {
        Sid      = "DecryptTraces"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = var.xray_kms_key_arn
        Include  = var.xray_kms_key_arn != ""
      },
    ] : { for key, value in statement : key => value if key != "Include" } if statement.Include
  ]
}

resource "terraform_data" "inputs_guard" {
  count = local.enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = contains(local.supported_regions, local.region)
      error_message = "Amazon Managed Grafana is not available in ${local.region}."
    }
    precondition {
      condition     = var.prometheus_workspace_arn != "" && var.prometheus_endpoint != ""
      error_message = "The workspace needs prometheus_workspace_arn and prometheus_endpoint. Enable the collector exports, which create the Prometheus workspace."
    }
    precondition {
      condition     = var.vpc_id != "" && length(var.subnet_ids) >= 2 && var.endpoints_security_group_id != ""
      error_message = "The workspace needs vpc_id, at least two subnet_ids in different Availability Zones, and endpoints_security_group_id."
    }
    precondition {
      condition     = length(var.admin_group_ids) > 0
      error_message = "The workspace needs at least one admin_group_ids entry, because only a Grafana Admin adds the data sources."
    }
  }
}

resource "aws_kms_key" "workspace" {
  count = local.customer_managed_key ? 1 : 0

  description         = "Encrypts the ${var.name_prefix} Grafana workspace."
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
    ]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-grafana" })
}

resource "aws_kms_alias" "workspace" {
  count = local.customer_managed_key ? 1 : 0

  name          = "alias/${var.name_prefix}-grafana"
  target_key_id = aws_kms_key.workspace[0].key_id
}

resource "aws_iam_role" "workspace" {
  count = local.enabled ? 1 : 0

  name        = "${var.name_prefix}-grafana"
  description = "Workspace role of the ${var.name_prefix} Grafana workspace. Queries the Prometheus workspace and reads X-Ray traces."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "grafana.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:grafana:${local.region}:${local.account_id}:/workspaces/*" }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "workspace" {
  count = local.enabled ? 1 : 0

  name = "data-sources"
  role = aws_iam_role.workspace[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.workspace_statements
  })
}

resource "aws_iam_role" "query" {
  count = local.query_role ? 1 : 0

  name        = "${var.name_prefix}-prometheus-query"
  description = "Queries the ${var.name_prefix} Prometheus workspace and cannot write to it."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.operator_principal_arns }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "query" {
  count = local.query_role ? 1 : 0

  name = "query"
  role = aws_iam_role.query[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "QueryTheWorkspace"
      Effect   = "Allow"
      Action   = local.prometheus_query_actions
      Resource = var.prometheus_workspace_arn
    }]
  })
}

resource "aws_security_group" "workspace" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-grafana-"
  description = "Grafana workspace network interfaces. No ingress. Egress to the VPC interface endpoints only."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-grafana" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  count = local.enabled ? 1 : 0

  security_group_id            = aws_security_group.workspace[0].id
  description                  = "HTTPS to the VPC interface endpoints for Prometheus and X-Ray"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

resource "aws_grafana_workspace" "this" {
  count = local.enabled ? 1 : 0

  name                     = "${var.name_prefix}-grafana"
  description              = "Dashboards over the ${var.name_prefix} collector's metrics and traces."
  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  permission_type          = "CUSTOMER_MANAGED"
  role_arn                 = aws_iam_role.workspace[0].arn
  data_sources             = ["PROMETHEUS", "XRAY"]
  grafana_version          = var.grafana_version
  kms_key_id               = local.customer_managed_key ? aws_kms_key.workspace[0].arn : null
  configuration            = jsonencode({ plugins = { pluginAdminEnabled = false }, unifiedAlerting = { enabled = true } })

  vpc_configuration {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.workspace[0].id]
  }

  dynamic "network_access_control" {
    for_each = length(var.allowed_vpc_endpoint_ids) > 0 ? [var.allowed_vpc_endpoint_ids] : []

    content {
      vpce_ids        = network_access_control.value
      prefix_list_ids = []
    }
  }

  tags = var.tags

  depends_on = [terraform_data.inputs_guard, aws_iam_role_policy.workspace, aws_vpc_security_group_egress_rule.endpoints]
}

resource "aws_grafana_role_association" "groups" {
  for_each = local.role_groups

  workspace_id = aws_grafana_workspace.this[0].id
  role         = each.key
  group_ids    = each.value
}
