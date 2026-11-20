# Images and packages for the gate, checks, and scanner pods, which DNS Firewall keeps from every
# public registry. aws mode mirrors images through ECR pull-through caches and resolves the
# package registries. external mode resolves only the registry config/<env>/registries.yaml names.
locals {
  registry_aws  = local.eks_enabled && var.registry_mode == "aws"
  registry_host = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.${data.aws_partition.current.dns_suffix}"

  # Each upstream needs no credential unless it is listed in registry_upstream_credentials.
  registry_upstreams = merge(
    {
      ecr-public = { url = "public.ecr.aws", mirrors = { "docker.io/library" = "docker/library", "public.ecr.aws" = "" } }
      quay       = { url = "quay.io", mirrors = { "quay.io" = "" } }
      k8s        = { url = "registry.k8s.io", mirrors = { "registry.k8s.io" = "" } }
    },
    contains(var.registry_upstream_credentials, "docker-hub") ? {
      docker-hub = { url = "registry-1.docker.io", mirrors = { "docker.io" = "" } }
    } : {},
    contains(var.registry_upstream_credentials, "ghcr") ? {
      ghcr = { url = "ghcr.io", mirrors = { "ghcr.io" = "" } }
    } : {},
  )
  registry_rules = local.registry_aws ? local.registry_upstreams : {}

  registry_mirrors = merge([
    for name, upstream in local.registry_rules : {
      for prefix, path in upstream.mirrors : prefix => trimsuffix("${local.registry_host}/${var.name_prefix}-${name}/${path}", "/")
    }
  ]...)

  registry_environment_config = local.registry_aws ? {
    "registries.yaml" = yamlencode({ config_version = 1, mode = "aws", mirrors = local.registry_mirrors })
  } : {}

  registry_cache_repository_arns = [
    for name in keys(local.registry_rules) :
    "arn:${data.aws_partition.current.partition}:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${var.name_prefix}-${name}/*"
  ]

  registry_domains = !local.eks_enabled ? [] : (
    var.registry_mode == "aws" ? var.registry_package_domains : (
      var.registry_mode == "external" ? compact([var.registry_external_host]) : []
    )
  )
}

data "aws_caller_identity" "current" {}

resource "aws_secretsmanager_secret" "registry_upstream" {
  for_each = toset(local.registry_aws ? var.registry_upstream_credentials : [])

  # ECR reads a pull-through credential only from a secret under this prefix.
  name                    = "ecr-pullthroughcache/${var.name_prefix}-${each.key}"
  description             = "Username and access token ECR presents to ${local.registry_upstreams[each.key].url} for the ${each.key} pull-through cache, as JSON with username and accessToken."
  recovery_window_in_days = var.secret_recovery_window_days

  tags = var.tags
}

resource "aws_ecr_pull_through_cache_rule" "registry" {
  for_each = local.registry_rules

  ecr_repository_prefix = "${var.name_prefix}-${each.key}"
  upstream_registry_url = each.value.url
  credential_arn        = try(aws_secretsmanager_secret.registry_upstream[each.key].arn, null)
}

# Without resource tags or a KMS key, ECR creates the repositories with its service-linked role.
# Either one would need a repository creation role, or the first pull of an image fails.
resource "aws_ecr_repository_creation_template" "registry" {
  for_each = local.registry_rules

  prefix               = "${var.name_prefix}-${each.key}"
  description          = "Repositories the ${each.key} pull-through cache creates on a first pull."
  applied_for          = ["PULL_THROUGH_CACHE"]
  image_tag_mutability = "MUTABLE"

  encryption_configuration {
    encryption_type = "AES256"
  }

  lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire cached images not pulled for 30 days."
      selection    = { tagStatus = "any", countType = "sinceImagePushed", countUnit = "days", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}
