locals {
  eks_enabled = var.create_eks

  # Secret store keys each EKS workload reads through the Secrets Store CSI driver. The database
  # keys exist only with Aurora, and the secret store rejects a reader key that names no secret.
  eks_workloads = {
    activity_worker = {
      service_account = "activity-worker"
      policy_arns     = concat(aws_iam_policy.eks_sandbox_image_pull[*].arn, aws_iam_policy.eks_temporal_ca[*].arn, aws_iam_policy.eks_registry_cache_fill[*].arn)
      secrets = [
        "database_platform_app", "coordination_shared_secret", "litellm_master_key", "authorship_signing_key",
        "review_attestation_secret", "github_token", "jira_api_token", "jira_pat", "slack_webhook_url",
        "registry_token",
      ]
    }
    litellm = {
      service_account = "litellm"
      policy_arns     = [module.bedrock_iam.invoke_policy_arn]
      secrets         = ["litellm_master_key", "langfuse_public_key", "langfuse_secret_key"]
    }
    mcp_gateway = {
      service_account = "mcp-gateway"
      policy_arns     = []
      secrets         = ["database_platform_app", "mcp_gateway_token"]
    }
    inbound_webhook = {
      service_account = "inbound-webhook"
      policy_arns     = aws_iam_policy.eks_temporal_ca[*].arn
      secrets         = ["database_platform_app", "github_webhook_secret", "jira_webhook_secret"]
    }
  }

  eks_secret_keys = distinct(flatten([
    for workload in values(local.eks_workloads) : [
      for key in workload.secrets : key if !startswith(key, "database_") || local.secret_store_database
    ]
  ]))
  eks_secret_readers = local.eks_enabled && var.create_secret_store ? {
    for key in local.eks_secret_keys : key => [
      for name, workload in local.eks_workloads : module.eks_cluster.pod_identity_role_arns[name] if contains(workload.secrets, key)
    ]
  } : {}

  # The Pod Identity agent reaches the eks-auth interface endpoint under this name.
  eks_domains = local.eks_enabled ? ["eks-auth.${var.region}.api.aws"] : []

  eks_activity_worker_role_arns = local.eks_enabled ? [module.eks_cluster.pod_identity_role_arns["activity_worker"]] : []
  # The activity worker and the inbound webhook start and signal Temporal workflows.
  eks_temporal_client_role_arns = local.eks_enabled ? [for name in ["activity_worker", "inbound_webhook"] : module.eks_cluster.pod_identity_role_arns[name]] : []
  eks_workspace                 = local.eks_enabled && module.container_services.workspace != null
  eks_deployer_enabled          = local.eks_enabled && var.create_ecs_cluster && var.create_container_services

  # AWS publishes the Load Balancer Controller image in the EKS registry of each region, which
  # DNS Firewall resolves. Most commercial regions use the 602401143452 account.
  eks_image_accounts = {
    "us-gov-west-1"  = "013241004608"
    "us-gov-east-1"  = "151742754352"
    "af-south-1"     = "877085696533"
    "ap-east-1"      = "800184023465"
    "ap-southeast-3" = "296578399912"
    "eu-south-1"     = "590381155156"
    "me-south-1"     = "558608220178"
  }
  eks_controller_repository = coalesce(
    var.eks_load_balancer_controller_image_repository,
    "${lookup(local.eks_image_accounts, var.region, "602401143452")}.dkr.ecr.${var.region}.${data.aws_partition.current.dns_suffix}/amazon/aws-load-balancer-controller",
  )

  eks_deploy_inputs = local.eks_deployer_enabled ? {
    cluster = {
      name                  = module.eks_cluster.cluster_name
      endpoint              = module.eks_cluster.cluster_endpoint
      certificate_authority = module.eks_cluster.cluster_certificate_authority_data
      region                = var.region
      vpc_id                = module.eks_cluster.vpc_id
    }
    images = {
      platform = var.container_platform_image
      opa      = var.eks_opa_image
      litellm  = var.eks_litellm_image
    }
    image_settings = {
      OPENHANDS_AGENT_SERVER_IMAGE = var.eks_agent_server_image
      GATE_ENGINE_BUILDER_IMAGE    = var.eks_buildah_image
      SEMGREP_IMAGE                = var.eks_semgrep_image
      TRIVY_IMAGE                  = var.eks_trivy_image
    }
    settings             = { LANGFUSE_HOST = "https://langfuse.${var.container_internal_domain}" }
    platform_environment = coalesce(module.container_services.platform_environment, {})
    environment_config   = merge(local.registry_environment_config, local.container_environment_config)
    security_groups = {
      cluster         = module.eks_cluster.cluster_security_group_id
      temporal_client = coalesce(module.temporal_control_plane.client_security_group_id, "")
      internal_client = try(module.container_services.internal_endpoints.client_security_group_id, "")
    }
    workspace_volume_handle = local.eks_workspace ? "${module.container_services.workspace.file_system_id}::${element(split("/", module.container_services.workspace.access_point_arns["checkouts"]), 1)}" : ""
    rebar_roles = {
      reader = coalesce(module.rebar_ticket_store.reader_role_arn, "")
      writer = coalesce(module.rebar_ticket_store.writer_role_arn, "")
    }
    secrets      = merge(module.secret_store.secret_arns, { temporal_ca = coalesce(module.temporal_control_plane.client_ca_secret_arn, "") })
    vpc_cidr     = data.aws_vpc.eks[0].cidr_block
    service_cidr = module.eks_cluster.service_cidr
    gate_egress_cidrs = concat(
      local.app_cidrs,
      [for entry in data.aws_ec2_managed_prefix_list.s3_eks[0].entries : entry.cidr],
      var.registry_mode == "external" ? var.registry_external_cidrs : [],
    )
    gate_egress_internet = var.registry_mode == "aws"
    ingress = {
      webhook_certificate_arn     = var.eks_webhook_certificate_arn
      mcp_gateway_certificate_arn = var.eks_mcp_gateway_certificate_arn
      mcp_gateway_client_cidrs    = var.eks_mcp_gateway_client_cidrs
    }
    node_pool_label = module.eks_cluster.node_pool_label
    load_balancer_controller = merge(module.eks_cluster.load_balancer_controller, {
      image_repository = local.eks_controller_repository
    })
  } : null
}

# The EKS deployments that hold a database connection restart after its credential rotates.
locals {
  eks_restart = local.eks_deployer_enabled && var.eks_deployer_image != "" && local.secret_store_database ? {
    cluster_arn        = module.ecs_cluster.cluster_arn
    task_family        = module.eks_deployer.family
    container          = module.eks_deployer.container
    subnet_ids         = local.management_subnet_ids
    security_group_ids = [module.eks_deployer.security_group_id]
    pass_role_arns     = [module.eks_deployer.execution_role_arn, module.eks_deployer.task_role_arn]
    deployments        = { database_platform_app = ["activity-worker", "inbound-webhook", "mcp-gateway"] }
  } : null
}

data "aws_partition" "current" {}

data "aws_vpc" "eks" {
  count = local.eks_deployer_enabled ? 1 : 0

  id = local.vpc_id
}

data "aws_ec2_managed_prefix_list" "s3_eks" {
  count = local.eks_deployer_enabled ? 1 : 0

  name = "com.amazonaws.${var.region}.s3"
}

module "eks_deployer" {
  source = "../../eks-deployer"

  create_eks_deployer         = local.eks_deployer_enabled
  name_prefix                 = var.name_prefix
  image                       = var.eks_deployer_image
  repository_arn              = lookup(module.container_services.repository_arns, "eks-deployer", "")
  cluster_arn                 = module.ecs_cluster.cluster_arn
  vpc_id                      = local.vpc_id
  subnet_ids                  = local.management_subnet_ids
  endpoints_security_group_id = local.endpoints_security_group_id
  deploy_inputs               = local.eks_deploy_inputs == null ? "{}" : jsonencode(local.eks_deploy_inputs)

  tags = var.tags
}

# The gate, checks, and scanner pods carry no AWS identity, so the activity worker requests the
# ECR token their in-pod pulls use, and its role bounds what that token can pull.
resource "aws_iam_policy" "eks_sandbox_image_pull" {
  count = local.eks_enabled && var.create_container_services ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-sandbox-image-pull-"
  description = "Lets the activity worker issue the ECR token the gate, checks, and scanner pods pull their images with."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "IssueRegistryTokens"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PullTheSandboxImages"
        Effect = "Allow"
        Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = concat(
          [for name in ["semgrep", "trivy"] : module.container_services.repository_arns[name]],
          var.eks_gate_image_repository_arns,
          local.registry_cache_repository_arns,
        )
      },
    ]
  })

  tags = var.tags
}

# The first pull of an image through a pull-through cache creates its repository and imports the
# image with the puller's identity, which is the activity worker's token in the gate pod.
resource "aws_iam_policy" "eks_registry_cache_fill" {
  count = local.eks_enabled && local.registry_aws ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-registry-cache-fill-"
  description = "Lets the gate, checks, and scanner pods fill the ECR pull-through caches on a first pull."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "FillThePullThroughCache"
      Effect   = "Allow"
      Action   = ["ecr:BatchImportUpstreamImage", "ecr:CreateRepository"]
      Resource = local.registry_cache_repository_arns
    }]
  })

  tags = var.tags
}

# The Temporal key admits its readers through IAM, so each EKS Temporal client decrypts the CA
# secret through an identity policy, as the ECS execution roles do.
resource "aws_iam_policy" "eks_temporal_ca" {
  count = local.eks_enabled && local.temporal_deployed ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-temporal-ca-"
  description = "Lets the EKS Temporal clients decrypt the Temporal CA secret through Secrets Manager."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DecryptTheTemporalCa"
      Effect    = "Allow"
      Action    = "kms:Decrypt"
      Resource  = module.temporal_control_plane.tls_kms_key_arn
      Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" } }
    }]
  })

  tags = var.tags
}

resource "aws_iam_policy" "eks_workspace_mount" {
  count = local.eks_workspace ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-workspace-mount-"
  description = "Lets the EFS CSI node driver mount the workspace checkouts access point read-write for the EKS pods."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "MountTheCheckoutsAccessPoint"
      Effect   = "Allow"
      Action   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
      Resource = module.container_services.workspace.file_system_arn
      Condition = {
        StringEquals = { "elasticfilesystem:AccessPointArn" = module.container_services.workspace.access_point_arns["checkouts"] }
      }
    }]
  })

  tags = var.tags
}

module "eks_cluster" {
  source = "../../eks-cluster"

  create_eks_cluster            = local.eks_enabled
  name_prefix                   = var.name_prefix
  kubernetes_version            = var.eks_kubernetes_version
  subnet_ids                    = local.subnet_ids
  api_access_security_group_ids = concat([module.bedrock_jumphost.task_security_group_id], local.eks_deployer_enabled ? [module.eks_deployer.security_group_id] : [])
  admin_principal_arns          = merge(var.eks_admin_principal_arns, local.eks_deployer_enabled ? { eks_deployer = module.eks_deployer.task_role_arn } : {})
  platform_node_group           = var.eks_platform_node_group
  sandbox_node_group            = var.eks_sandbox_node_group
  network_policy_enforcing_mode = var.eks_network_policy_enforcing_mode
  use_fips_endpoints            = var.container_use_fips_endpoints
  addon_versions                = var.eks_addon_versions

  workloads                = { for name, workload in local.eks_workloads : name => { service_account = workload.service_account, policy_arns = workload.policy_arns } }
  efs_csi_node_policy_arns = aws_iam_policy.eks_workspace_mount[*].arn

  tags = var.tags
}
