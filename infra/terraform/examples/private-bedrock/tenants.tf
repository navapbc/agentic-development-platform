locals {
  tenants_enabled = local.eks_enabled
  tenants         = local.tenants_enabled ? var.tenants : {}

  # Each tenant's activity worker runs as the activity-worker service account of its own
  # namespace. Its policies are attached below, outside the cluster module, because the tenant
  # module needs the role first.
  tenant_workloads = {
    for name in keys(local.tenants) : "tenant_${name}" => {
      service_account = "activity-worker"
      namespace       = "tenant-${name}"
    }
  }

  github_oidc_provider_arn = coalesce(var.github_oidc_provider_arn, module.github_image_publisher.oidc_provider_arn, "none")

  tenant_secret_readers = var.create_secret_store ? {
    for key in distinct(flatten([for tenant in values(local.tenants) : tenant.shared_secrets])) : key => [
      for name, tenant in local.tenants : module.eks_cluster.pod_identity_role_arns["tenant_${name}"] if contains(tenant.shared_secrets, key)
    ]
  } : {}

  tenant_records = { for name, tenant in module.tenant : name => tenant.tenant }

  # The platform secrets every tenant worker reads, besides the tenant's own and its shared ones.
  # The database key exists only with Aurora.
  tenant_platform_secrets = [
    for key in ["database_platform_app", "coordination_shared_secret", "litellm_master_key", "review_attestation_secret"] :
    key if !startswith(key, "database_") || local.secret_store_database
  ]
  tenant_platform_secret_readers = local.tenants_enabled && var.create_secret_store ? {
    for key in local.tenant_platform_secrets : key => tolist([for name in keys(local.tenants) : module.eks_cluster.pod_identity_role_arns["tenant_${name}"]])
  } : {}

  tenant_worker_policy_arns = merge(
    { for policy in aws_iam_policy.eks_sandbox_image_pull : "sandbox-image-pull" => policy.arn },
    { for policy in aws_iam_policy.eks_temporal_ca : "temporal-ca" => policy.arn },
    { for policy in aws_iam_policy.eks_registry_cache_fill : "registry-cache-fill" => policy.arn },
  )
  tenant_worker_policies = {
    for pair in setproduct(keys(local.tenants), keys(local.tenant_worker_policy_arns)) :
    "${pair[0]}-${pair[1]}" => { tenant = pair[0], policy_arn = local.tenant_worker_policy_arns[pair[1]] }
  }

  eks_tenant_inputs = [
    for name, tenant in local.tenants : {
      name                    = name
      workspace_volume_handle = local.eks_workspace ? "${module.container_services.workspace.file_system_id}::${element(split("/", module.container_services.workspace.tenant_access_point_arns[name]), 1)}" : ""
      rebar_roles = {
        reader = local.tenant_records[name].ticket_store.reader_role_arn
        writer = local.tenant_records[name].ticket_store.writer_role_arn
      }
      secrets          = local.tenant_records[name].secret_arns
      platform_secrets = distinct(concat(local.tenant_platform_secrets, tenant.shared_secrets, local.temporal_deployed ? ["temporal_ca"] : []))
      settings         = tenant.settings
      sandbox_quota    = tenant.sandbox_quota
      config_bucket    = module.tenant_config_store.bucket
    }
  ]
}

resource "terraform_data" "tenants_guard" {
  count = local.tenants_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = length(var.tenants) > 0
      error_message = "create_eks needs at least one entry in tenants. Every run belongs to a tenant, and the deployment has no default tenant."
    }
    precondition {
      condition     = var.create_container_services && var.create_secret_store
      error_message = "Tenants need create_container_services for their workspaces and images and create_secret_store for their shared secrets."
    }
    precondition {
      condition     = alltrue([for tenant in values(var.tenants) : alltrue([for key in tenant.shared_secrets : contains(keys(module.secret_store.secret_arns), key)])])
      error_message = "A tenant's shared_secrets names a key the secret store does not hold."
    }
    precondition {
      condition     = alltrue([for tenant in values(var.tenants) : tenant.config_repository == null]) || local.github_oidc_provider_arn != "none"
      error_message = "A tenant with a config_repository needs the account's GitHub OIDC provider. Set github_oidc_provider_arn, or create_github_image_publisher to create the provider."
    }
  }
}

module "tenant_config_store" {
  source = "../../tenant-config-store"

  create_tenant_config_store = local.tenants_enabled && length(var.tenants) > 0
  name_prefix                = var.name_prefix
  access_log_bucket          = var.access_log_bucket
  admin_principals           = var.tenant_config_admin_principals
  tenants = {
    for name, tenant in local.tenants : name => {
      publishers = tenant.config_repository == null ? [] : ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-tenant-${name}-publisher"]
      readers    = [module.eks_cluster.pod_identity_role_arns["tenant_${name}"]]
    }
  }
  tags = var.tags
}

module "tenant" {
  source   = "../../tenant"
  for_each = local.tenants

  create_tenant               = true
  name_prefix                 = var.name_prefix
  tenant                      = each.key
  description                 = each.value.description
  worker_role_arn             = module.eks_cluster.pod_identity_role_arns["tenant_${each.key}"]
  secrets                     = each.value.secrets
  secret_recovery_window_days = var.secret_recovery_window_days
  access_log_bucket           = var.access_log_bucket
  ticket_store                = each.value.ticket_store
  config_store = {
    bucket_arn  = module.tenant_config_store.bucket_arn
    kms_key_arn = module.tenant_config_store.kms_key_arn
  }
  config_publisher = each.value.config_repository == null ? null : {
    oidc_provider_arn     = local.github_oidc_provider_arn
    github_repository     = each.value.config_repository.github_repository
    allowed_refs          = each.value.config_repository.allowed_refs
    github_repository_ids = each.value.config_repository.github_repository_ids
  }
  tags = var.tags
}

# The shared EKS policies attach by role name here, as the tenant policy does, so the roles and
# policies do not form a cycle through the cluster module.
resource "aws_iam_role_policy_attachment" "tenant_worker_shared" {
  for_each = local.tenant_worker_policies

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["tenant_${each.value.tenant}"]), 1)
  policy_arn = each.value.policy_arn

  depends_on = [module.eks_cluster]
}

resource "aws_iam_role_policy_attachment" "tenant_worker" {
  for_each = local.tenants

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["tenant_${each.key}"]), 1)
  policy_arn = module.tenant[each.key].tenant.worker_policy_arn

  depends_on = [module.eks_cluster]
}
