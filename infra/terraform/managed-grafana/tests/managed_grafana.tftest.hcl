mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/agentic-platform-role" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111111111111:key/grafana" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-grafana" }
  }
  mock_resource "aws_grafana_workspace" {
    defaults = { id = "g-1", endpoint = "g-1.grafana-workspace.us-east-1.amazonaws.com" }
  }
}

override_resource {
  target = aws_iam_role.workspace
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-grafana" }
}

override_resource {
  target = aws_iam_role.query
  values = { arn = "arn:aws:iam::111111111111:role/agentic-platform-prometheus-query" }
}

variables {
  name_prefix                 = "agentic-platform"
  prometheus_workspace_arn    = "arn:aws:aps:us-east-1:111111111111:workspace/ws-telemetry"
  prometheus_endpoint         = "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-telemetry/"
  vpc_id                      = "vpc-1"
  subnet_ids                  = ["subnet-a1", "subnet-a2"]
  endpoints_security_group_id = "sg-endpoints"
  admin_group_ids             = ["group-admins"]
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition = (
      length(aws_grafana_workspace.this) == 0 && length(aws_grafana_role_association.groups) == 0 &&
      length(aws_iam_role.workspace) == 0 && length(aws_iam_role_policy.workspace) == 0 &&
      length(aws_iam_role.query) == 0 && length(aws_iam_role_policy.query) == 0 &&
      length(aws_security_group.workspace) == 0 && length(aws_vpc_security_group_egress_rule.endpoints) == 0 &&
      length(aws_kms_key.workspace) == 0 && length(aws_kms_alias.workspace) == 0 && length(terraform_data.inputs_guard) == 0
    )
    error_message = "The disabled module planned resources."
  }

  assert {
    condition = (
      output.workspace_id == null && output.workspace_url == null && output.query_role_arn == null &&
      output.workspace_role_arn == null && output.data_sources == null && length(output.interface_services) == 0
    )
    error_message = "The disabled module returned a workspace."
  }
}

run "disabled_module_needs_no_inputs" {
  command = plan

  variables {
    prometheus_workspace_arn    = ""
    prometheus_endpoint         = ""
    subnet_ids                  = []
    endpoints_security_group_id = ""
    admin_group_ids             = []
  }

  assert {
    condition     = length(aws_grafana_workspace.this) == 0
    error_message = "The disabled module planned a workspace."
  }
}

run "workspace_signs_in_through_identity_center_and_reads_both_data_sources" {
  command = apply

  variables {
    create_managed_grafana = true
    viewer_group_ids       = ["group-viewers"]
  }

  assert {
    condition = (
      aws_grafana_workspace.this[0].authentication_providers == tolist(["AWS_SSO"]) &&
      aws_grafana_workspace.this[0].account_access_type == "CURRENT_ACCOUNT" &&
      aws_grafana_workspace.this[0].permission_type == "CUSTOMER_MANAGED" &&
      aws_grafana_workspace.this[0].role_arn == "arn:aws:iam::111111111111:role/agentic-platform-grafana"
    )
    error_message = "The workspace does not sign in through IAM Identity Center with the module's role."
  }

  assert {
    condition     = toset(aws_grafana_workspace.this[0].data_sources) == toset(["PROMETHEUS", "XRAY"])
    error_message = "The workspace does not enable the Prometheus and X-Ray data sources."
  }

  assert {
    condition     = jsondecode(aws_grafana_workspace.this[0].configuration).plugins.pluginAdminEnabled == false
    error_message = "The workspace lets an admin install plugins."
  }

  assert {
    condition     = aws_grafana_workspace.this[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/grafana" && aws_kms_key.workspace[0].enable_key_rotation
    error_message = "The workspace is not encrypted under the module's rotating key."
  }

  assert {
    condition     = length(aws_grafana_workspace.this[0].network_access_control) == 0
    error_message = "Network access control was set without a VPC endpoint."
  }

  assert {
    condition = (
      keys(aws_grafana_role_association.groups) == ["ADMIN", "VIEWER"] &&
      aws_grafana_role_association.groups["ADMIN"].group_ids == toset(["group-admins"]) &&
      aws_grafana_role_association.groups["VIEWER"].group_ids == toset(["group-viewers"])
    )
    error_message = "The Identity Center groups are not associated with their Grafana roles."
  }

  assert {
    condition = (
      output.data_sources.prometheus.url == "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-telemetry" &&
      output.data_sources.prometheus.assume_role_arn == output.query_role_arn &&
      output.data_sources.xray.region == "us-east-1" &&
      output.workspace_url == "https://g-1.grafana-workspace.us-east-1.amazonaws.com"
    )
    error_message = "The data source settings do not name the workspace endpoint and the query role."
  }
}

run "workspace_traffic_leaves_only_through_the_interface_endpoints" {
  command = apply

  variables {
    create_managed_grafana = true
  }

  assert {
    condition = (
      one(aws_grafana_workspace.this[0].vpc_configuration).subnet_ids == toset(["subnet-a1", "subnet-a2"]) &&
      one(aws_grafana_workspace.this[0].vpc_configuration).security_group_ids == toset(["sg-grafana"])
    )
    error_message = "The workspace is not connected to the application subnets under its own group."
  }

  assert {
    condition     = toset(output.interface_services) == toset(["aps-workspaces", "grafana-workspace", "sts", "xray"])
    error_message = "The interface endpoints do not cover every AWS service the workspace calls."
  }

  assert {
    condition = (
      length(aws_security_group.workspace[0].ingress) == 0 &&
      aws_vpc_security_group_egress_rule.endpoints[0].referenced_security_group_id == "sg-endpoints" &&
      aws_vpc_security_group_egress_rule.endpoints[0].from_port == 443 &&
      aws_vpc_security_group_egress_rule.endpoints[0].to_port == 443 &&
      aws_vpc_security_group_egress_rule.endpoints[0].cidr_ipv4 == null
    )
    error_message = "The workspace group admits ingress or reaches a destination other than the endpoints."
  }
}

run "query_role_queries_the_workspace_and_cannot_write" {
  command = apply

  variables {
    create_managed_grafana  = true
    operator_principal_arns = ["arn:aws:iam::111111111111:role/operators"]
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.query[0].policy).Statement :
      statement.Effect == "Allow" && statement.Resource == "arn:aws:aps:us-east-1:111111111111:workspace/ws-telemetry" &&
      length(setsubtract(toset(statement.Action), toset(["aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"]))) == 0
    ])
    error_message = "The query role holds an action beyond the four query actions, or a resource beyond the workspace."
  }

  assert {
    condition = alltrue([
      for action in flatten([for statement in jsondecode(aws_iam_role_policy.query[0].policy).Statement : statement.Action]) :
      length(regexall("(?i)^aps:(RemoteWrite|Create|Put|Update|Delete|Tag|Untag)|\\*", action)) == 0
    ])
    error_message = "The query role may write to the workspace."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role.query[0].assume_role_policy).Statement[0].Principal.AWS) == toset([
      "arn:aws:iam::111111111111:role/agentic-platform-grafana",
      "arn:aws:iam::111111111111:role/operators",
    ])
    error_message = "The query role trusts a principal other than the workspace role and the operators."
  }
}

run "workspace_role_reads_traces_and_assumes_only_the_query_role" {
  command = apply

  variables {
    create_managed_grafana = true
  }

  assert {
    condition = (
      [for statement in jsondecode(aws_iam_role_policy.workspace[0].policy).Statement : statement.Resource if statement.Action == "sts:AssumeRole"] ==
      ["arn:aws:iam::111111111111:role/agentic-platform-prometheus-query"]
    )
    error_message = "The workspace role may assume a role other than the query role."
  }

  assert {
    condition = alltrue([
      for action in flatten([for statement in jsondecode(aws_iam_role_policy.workspace[0].policy).Statement : statement.Action]) :
      can(regex("^(xray:(BatchGet|Get)[A-Za-z]+|sts:AssumeRole|ec2:DescribeRegions)$", action))
    ])
    error_message = "The workspace role holds an action other than trace reads, region listing, and the query role assumption."
  }

  assert {
    condition     = length([for statement in jsondecode(aws_iam_role_policy.workspace[0].policy).Statement : statement if statement.Sid == "DecryptTraces"]) == 0
    error_message = "The workspace role may decrypt without an X-Ray key."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.workspace[0].assume_role_policy).Statement[0].Principal.Service == "grafana.amazonaws.com" &&
      jsondecode(aws_iam_role.workspace[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "111111111111"
    )
    error_message = "The workspace role trusts a principal other than this account's Grafana workspaces."
  }
}

run "encrypted_traces_are_decrypted_with_the_xray_key_only" {
  command = apply

  variables {
    create_managed_grafana = true
    xray_kms_key_arn       = "arn:aws:kms:us-east-1:111111111111:key/telemetry"
  }

  assert {
    condition = (
      [for statement in jsondecode(aws_iam_role_policy.workspace[0].policy).Statement : [statement.Action, statement.Resource] if statement.Sid == "DecryptTraces"] ==
      [["kms:Decrypt", "arn:aws:kms:us-east-1:111111111111:key/telemetry"]]
    )
    error_message = "The workspace role does not decrypt with the X-Ray key alone."
  }
}

run "vpc_endpoints_restrict_who_reaches_the_workspace" {
  command = apply

  variables {
    create_managed_grafana   = true
    allowed_vpc_endpoint_ids = ["vpce-grafana"]
  }

  assert {
    condition = (
      one(aws_grafana_workspace.this[0].network_access_control).vpce_ids == toset(["vpce-grafana"]) &&
      length(one(aws_grafana_workspace.this[0].network_access_control).prefix_list_ids) == 0
    )
    error_message = "The workspace admits networks other than the grafana-workspace endpoint."
  }
}

run "govcloud_workspace_uses_the_aws_owned_key" {
  command   = apply
  state_key = "govcloud"

  override_data {
    target = data.aws_partition.current
    values = { partition = "aws-us-gov" }
  }

  override_data {
    target = data.aws_region.current
    values = { region = "us-gov-west-1" }
  }

  variables {
    create_managed_grafana = true
  }

  assert {
    condition     = length(aws_kms_key.workspace) == 0 && length(aws_kms_alias.workspace) == 0 && output.kms_key_arn == null
    error_message = "A customer managed key was planned where Amazon Managed Grafana does not accept one."
  }
}

run "unsupported_region_is_refused" {
  command = plan

  override_data {
    target = data.aws_region.current
    values = { region = "ca-central-1" }
  }

  variables {
    create_managed_grafana = true
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "workspace_without_the_prometheus_workspace_is_refused" {
  command = plan

  variables {
    create_managed_grafana   = true
    prometheus_workspace_arn = ""
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "workspace_in_one_subnet_is_refused" {
  command = plan

  variables {
    create_managed_grafana = true
    subnet_ids             = ["subnet-a1"]
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "workspace_without_an_admin_group_is_refused" {
  command = plan

  variables {
    create_managed_grafana = true
    admin_group_ids        = []
  }

  expect_failures = [terraform_data.inputs_guard]
}
