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

resource "aws_iam_role_policy_attachment" "tenant_worker" {
  for_each = local.tenants

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["tenant_${each.key}"]), 1)
  policy_arn = module.tenant[each.key].tenant.worker_policy_arn

  depends_on = [module.eks_cluster]
}
