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
}

override_resource {
  target          = aws_kms_key.secrets
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/secrets" }
}

override_resource {
  target          = aws_kms_key.logs
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/logs" }
}

override_resource {
  target          = aws_kms_key.ebs
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/ebs" }
}

override_resource {
  target          = aws_iam_role.pod_identity
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/pod-identity" }
}

variables {
  name_prefix = "agentic-platform"
  subnet_ids  = ["subnet-a", "subnet-b"]
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition = (
      length(aws_eks_cluster.this) == 0 && length(aws_kms_key.secrets) == 0 && length(aws_kms_key.logs) == 0 &&
      length(aws_kms_key.ebs) == 0 && length(aws_cloudwatch_log_group.cluster) == 0 && length(aws_iam_role.cluster) == 0 &&
      length(aws_iam_role.node) == 0 && length(aws_iam_role.pod_identity) == 0 && length(aws_launch_template.node) == 0 &&
      length(aws_eks_node_group.this) == 0 && length(aws_eks_addon.before_nodes) == 0 && length(aws_eks_addon.after_nodes) == 0 &&
      length(aws_eks_pod_identity_association.this) == 0 && length(aws_iam_role_policy_attachment.pod_identity) == 0 &&
      length(aws_iam_role_policy.load_balancer_controller) == 0
    )
    error_message = "The disabled module planned resources."
  }

  assert {
    condition     = output.load_balancer_controller == null && output.pod_identity_role_arns == {}
    error_message = "The disabled module reported a controller or Pod Identity roles."
  }
}

run "control_plane_is_private_encrypted_and_logged" {
  command = plan

  variables {
    create_eks_cluster = true
  }

  assert {
    condition     = aws_eks_cluster.this[0].name == "agentic-platform-eks" && aws_eks_cluster.this[0].version == "1.37"
    error_message = "The cluster is not named <name_prefix>-eks at the default version."
  }

  assert {
    condition     = aws_eks_cluster.this[0].vpc_config[0].endpoint_private_access && !aws_eks_cluster.this[0].vpc_config[0].endpoint_public_access
    error_message = "The API endpoint is not private-only."
  }

  assert {
    condition = (
      one(aws_eks_cluster.this[0].encryption_config[*].resources) == toset(["secrets"]) &&
      aws_eks_cluster.this[0].encryption_config[0].provider[0].key_arn == "arn:aws:kms:us-east-1:111111111111:key/secrets"
    )
    error_message = "Kubernetes secrets are not envelope-encrypted under the secrets key."
  }

  assert {
    condition     = aws_eks_cluster.this[0].enabled_cluster_log_types == toset(["api", "audit", "authenticator"])
    error_message = "The control plane does not log exactly the api, audit, and authenticator types."
  }

  assert {
    condition = (
      aws_cloudwatch_log_group.cluster[0].name == "/aws/eks/agentic-platform-eks/cluster" &&
      aws_cloudwatch_log_group.cluster[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/logs" &&
      aws_cloudwatch_log_group.cluster[0].retention_in_days == 365
    )
    error_message = "The control plane log group is not the EKS group encrypted under the logs key."
  }

  assert {
    condition = (
      jsondecode(aws_kms_key.logs[0].policy).Statement[1].Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"] ==
      "arn:aws:logs:us-east-1:111111111111:log-group:/aws/eks/agentic-platform-eks/cluster"
    )
    error_message = "The logs key admits CloudWatch Logs beyond the control plane log group."
  }

  assert {
    condition = (
      aws_eks_cluster.this[0].access_config[0].authentication_mode == "API" &&
      !aws_eks_cluster.this[0].access_config[0].bootstrap_cluster_creator_admin_permissions &&
      !aws_eks_cluster.this[0].bootstrap_self_managed_addons &&
      aws_eks_cluster.this[0].upgrade_policy[0].support_type == "STANDARD"
    )
    error_message = "Access is not through access entries only, or the cluster installs self-managed add-ons or extended support."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.cluster_secrets_key[0].policy).Statement[0].Resource == "arn:aws:kms:us-east-1:111111111111:key/secrets" &&
      length(aws_eks_access_entry.admin) == 0 && length(aws_vpc_security_group_ingress_rule.api_access) == 0
    )
    error_message = "The cluster role does not use the secrets key, or access was granted without being requested."
  }
}

run "access_entries_and_api_rules_follow_the_inputs" {
  command = plan

  variables {
    create_eks_cluster            = true
    admin_principal_arns          = { operator = "arn:aws:iam::111111111111:role/operator" }
    api_access_security_group_ids = ["sg-jumphost"]
  }

  assert {
    condition = (
      aws_eks_access_entry.admin["operator"].principal_arn == "arn:aws:iam::111111111111:role/operator" &&
      aws_eks_access_policy_association.admin["operator"].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" &&
      aws_eks_access_policy_association.admin["operator"].access_scope[0].type == "cluster"
    )
    error_message = "The admin principal is not granted the cluster admin access policy."
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.api_access[0].referenced_security_group_id == "sg-jumphost" &&
      aws_vpc_security_group_ingress_rule.api_access[0].from_port == 443 && aws_vpc_security_group_ingress_rule.api_access[0].to_port == 443 &&
      aws_vpc_security_group_egress_rule.api_access[0].security_group_id == "sg-jumphost" && aws_vpc_security_group_egress_rule.api_access[0].to_port == 443
    )
    error_message = "The operator security group is not admitted to the API, and allowed out to it, on 443 only."
  }
}

run "nodes_are_arm64_with_imdsv2_and_encrypted_volumes" {
  command = plan

  variables {
    create_eks_cluster = true
  }

  assert {
    condition = alltrue([
      for template in aws_launch_template.node : (
        template.metadata_options[0].http_tokens == "required" && template.metadata_options[0].http_put_response_hop_limit == 1 &&
        template.block_device_mappings[0].ebs[0].encrypted == "true" &&
        template.block_device_mappings[0].ebs[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/ebs"
      )
    ])
    error_message = "A node launch template does not require IMDSv2 at a hop limit of 1 or encrypt its volume under the EBS key."
  }

  assert {
    condition     = alltrue([for group in aws_eks_node_group.this : group.ami_type == "AL2023_ARM_64_STANDARD"])
    error_message = "A node group is not arm64."
  }

  assert {
    condition = (
      length(aws_eks_node_group.this["platform"].taint) == 0 &&
      one(aws_eks_node_group.this["sandbox"].taint).key == "agentic-dev-platform/sandbox" &&
      one(aws_eks_node_group.this["sandbox"].taint).effect == "NO_SCHEDULE" &&
      aws_eks_node_group.this["sandbox"].labels["agentic-dev-platform/node-pool"] == "sandbox"
    )
    error_message = "Only the sandbox node group carries the NoSchedule sandbox taint."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_kms_key.ebs[0].policy).Statement :
      statement.Condition["ArnEquals"]["aws:PrincipalArn"] == "arn:aws:iam::111111111111:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
      if statement.Principal == { AWS = "*" }
    ])
    error_message = "A statement of the EBS key admits a principal other than the Auto Scaling service-linked role."
  }
}

run "pod_identity_roles_are_bound_to_their_service_accounts" {
  command = plan

  variables {
    create_eks_cluster = true
    workloads = {
      activity_worker = { service_account = "activity-worker", policy_arns = ["arn:aws:iam::111111111111:policy/rebar-ticket-store"] }
      litellm         = { service_account = "litellm", policy_arns = ["arn:aws:iam::111111111111:policy/agentic-platform-bedrock-invoke"] }
      inbound_webhook = { service_account = "inbound-webhook" }
      tenant_adp      = { service_account = "activity-worker", namespace = "tenant-adp" }
    }
  }

  assert {
    condition     = keys(aws_iam_role.pod_identity) == ["activity_worker", "efs_csi_controller", "efs_csi_node", "inbound_webhook", "litellm", "load_balancer_controller", "tenant_adp", "vpc_cni"]
    error_message = "The module does not create a Pod Identity role for each workload and kube-system service account."
  }

  assert {
    condition = alltrue([
      for name, role in aws_iam_role.pod_identity : (
        jsondecode(role.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:eks:us-east-1:111111111111:cluster/agentic-platform-eks" &&
        jsondecode(role.assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
      )
    ])
    error_message = "A Pod Identity role trusts a cluster other than this one."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.pod_identity["litellm"].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "agentic-dev-platform" &&
      jsondecode(aws_iam_role.pod_identity["litellm"].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "litellm" &&
      jsondecode(aws_iam_role.pod_identity["vpc_cni"].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "aws-node" &&
      jsondecode(aws_iam_role.pod_identity["tenant_adp"].assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "tenant-adp" &&
      aws_eks_pod_identity_association.this["tenant_adp"].namespace == "tenant-adp"
    )
    error_message = "A Pod Identity role is not bound to its namespace and service account."
  }

  assert {
    condition = (
      aws_iam_role_policy_attachment.pod_identity["litellm-0"].policy_arn == "arn:aws:iam::111111111111:policy/agentic-platform-bedrock-invoke" &&
      aws_iam_role_policy_attachment.pod_identity["activity_worker-0"].policy_arn == "arn:aws:iam::111111111111:policy/rebar-ticket-store" &&
      aws_iam_role_policy_attachment.pod_identity["vpc_cni-0"].policy_arn == "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy" &&
      aws_iam_role_policy_attachment.pod_identity["efs_csi_controller-0"].policy_arn == "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy" &&
      length(aws_iam_role_policy_attachment.pod_identity) == 4
    )
    error_message = "The Pod Identity roles do not carry exactly their policies."
  }

  assert {
    condition = (
      keys(aws_eks_pod_identity_association.this) == ["activity_worker", "inbound_webhook", "litellm", "load_balancer_controller", "tenant_adp"] &&
      aws_eks_pod_identity_association.this["load_balancer_controller"].namespace == "kube-system" &&
      aws_eks_pod_identity_association.this["load_balancer_controller"].service_account == "aws-load-balancer-controller" &&
      [for association in aws_eks_addon.before_nodes["vpc-cni"].pod_identity_association : association.service_account] == ["aws-node"] &&
      toset([for association in aws_eks_addon.after_nodes["aws-efs-csi-driver"].pod_identity_association : association.service_account]) == toset(["efs-csi-controller-sa", "efs-csi-node-sa"])
    )
    error_message = "A Pod Identity role is not associated with its service account."
  }

  assert {
    condition = (
      contains(flatten([for s in jsondecode(aws_iam_role_policy.load_balancer_controller[0].policy).Statement : s.Action]), "elasticloadbalancing:CreateLoadBalancer") &&
      strcontains(aws_iam_role_policy.load_balancer_controller[0].policy, "arn:aws:elasticloadbalancing:")
    )
    error_message = "The Load Balancer Controller role does not carry the controller policy."
  }
}

run "controller_policy_follows_the_partition" {
  command = plan

  variables {
    create_eks_cluster = true
  }

  override_data {
    target = data.aws_partition.current
    values = { partition = "aws-us-gov" }
  }

  assert {
    condition = (
      strcontains(aws_iam_role_policy.load_balancer_controller[0].policy, "arn:aws-us-gov:elasticloadbalancing:") &&
      !strcontains(aws_iam_role_policy.load_balancer_controller[0].policy, "arn:aws:")
    )
    error_message = "The Load Balancer Controller policy names ARNs outside the partition."
  }
}

run "network_policy_starts_standard_and_switches_to_strict" {
  command = plan

  variables {
    create_eks_cluster = true
  }

  assert {
    condition = (
      jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).enableNetworkPolicy == "true" &&
      jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).env.NETWORK_POLICY_ENFORCING_MODE == "standard"
    )
    error_message = "The VPC CNI does not enforce network policy in standard mode by default."
  }

  assert {
    condition = (
      jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).env.ENABLE_POD_ENI == "true" &&
      jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).env.POD_SECURITY_GROUP_ENFORCING_MODE == "standard" &&
      contains(keys(aws_iam_role_policy_attachment.cluster), "AmazonEKSVPCResourceController")
    )
    error_message = "Security groups for pods are not enabled in the standard enforcing mode that Pod Identity requires."
  }

  assert {
    condition     = keys(aws_eks_addon.before_nodes) == ["eks-pod-identity-agent", "kube-proxy", "vpc-cni"] && keys(aws_eks_addon.after_nodes) == ["aws-efs-csi-driver", "aws-secrets-store-csi-driver-provider", "coredns"]
    error_message = "The add-ons are not split into those the nodes need and those that need nodes."
  }
}

run "strict_mode_reaches_the_vpc_cni" {
  command = plan

  variables {
    create_eks_cluster            = true
    network_policy_enforcing_mode = "strict"
    use_fips_endpoints            = true
  }

  assert {
    condition     = jsondecode(aws_eks_addon.before_nodes["vpc-cni"].configuration_values).env.NETWORK_POLICY_ENFORCING_MODE == "strict"
    error_message = "The VPC CNI is not in strict mode."
  }

  assert {
    condition     = jsondecode(aws_eks_addon.after_nodes["aws-secrets-store-csi-driver-provider"].configuration_values).useFipsEndpoint
    error_message = "The Secrets Store CSI provider does not call the FIPS endpoint."
  }
}

run "unknown_network_policy_mode_is_rejected" {
  command = plan

  variables {
    network_policy_enforcing_mode = "monitor"
  }

  expect_failures = [var.network_policy_enforcing_mode]
}

run "x86_instance_types_are_rejected" {
  command = plan

  variables {
    sandbox_node_group = { instance_types = ["m7i.large"] }
  }

  expect_failures = [var.sandbox_node_group]
}

run "one_subnet_is_rejected" {
  command = plan

  variables {
    create_eks_cluster = true
    subnet_ids         = ["subnet-a"]
  }

  expect_failures = [aws_eks_cluster.this]
}
