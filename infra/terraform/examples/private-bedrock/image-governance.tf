# The images a tenant pod may run. The composition generates image_policy.yaml into the platform
# config, scans every ECR image with Amazon Inspector when image_scanning_enabled is true, and
# signs the platform's catalog images with its own Signer profile. See docs/image-governance.md.
locals {
  image_governance = local.eks_enabled && var.create_container_services

  # The repositories of the platform catalog, which a tenant pod may run and the platform signs.
  image_catalog_repositories = [
    "semgrep",
    "trivy",
    "gate-engine-security-policy",
    "gate-engine-test-policy",
    "openhands-agent-server",
    "agent-server-jvm",
    "agent-server-go",
    "checks-python",
    "checks-node",
    "checks-jvm",
    "checks-go",
    "buildah",
  ]
  image_catalog = [for name in local.image_catalog_repositories : "${local.registry_host}/${var.name_prefix}/${name}"]
  image_caches  = [for name in keys(local.registry_rules) : "${local.registry_host}/${var.name_prefix}-${name}/"]

  image_tenant_prefix = "${local.registry_host}/${var.name_prefix}/tenants/{tenant}/"
  image_policy = {
    config_version      = 1
    platform_registries = [local.registry_host]
    catalog             = local.image_catalog
    tenant_prefix       = local.image_tenant_prefix
    caches              = local.image_caches
    registries          = var.image_policy_registries
    scan = {
      gate     = var.image_scanning_enabled ? "inspector" : "off"
      severity = var.image_finding_severity
    }
    signatures = {
      verification     = var.image_signature_verification
      platform_profile = try(aws_signer_signing_profile.platform[0].arn, "")
      tenant_profiles  = { for name, tenant in local.tenant_records : name => tenant.signing_profile }
    }
  }
  image_environment_config = local.image_governance ? { "image_policy.yaml" = yamlencode(local.image_policy) } : {}

  # The kubelet pulls a sandbox pod's image as written, so each tenant's sandbox namespace admits
  # the catalog, its own prefix, the caches, and the listed registries.
  image_prefixes = {
    for name in keys(local.tenants) : name => concat(
      local.image_catalog,
      [replace(local.image_tenant_prefix, "{tenant}", name)],
      local.image_caches,
      var.image_policy_registries,
    )
  }
}

resource "aws_signer_signing_profile" "platform" {
  count = local.image_governance ? 1 : 0

  # Signer cancels a destroyed profile and never frees its name, so the name is generated.
  name_prefix = "${replace(var.name_prefix, "-", "_")}_platform_"
  platform_id = "Notation-OCI-SHA384-ECDSA"

  tags = var.tags
}

resource "aws_inspector2_enabler" "ecr" {
  count = local.image_governance && var.image_scanning_enabled ? 1 : 0

  account_ids    = [data.aws_caller_identity.current.account_id]
  resource_types = ["ECR"]
}

resource "aws_ecr_registry_scanning_configuration" "enhanced" {
  count = local.image_governance && var.image_scanning_enabled ? 1 : 0

  scan_type = "ENHANCED"

  rule {
    scan_frequency = "CONTINUOUS_SCAN"
    repository_filter {
      filter      = "*"
      filter_type = "WILDCARD"
    }
  }

  depends_on = [aws_inspector2_enabler.ecr]
}

# The tenant worker reads the scan of each image before a pod pulls it. Inspector's read actions
# take no resource, so the worker reads every image's scan, which exposes finding metadata only.
resource "aws_iam_policy" "image_scan_read" {
  count = local.image_governance && var.image_scanning_enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-image-scan-read-"
  description = "Lets a tenant worker read the Amazon Inspector coverage and findings of an image before a pod pulls it."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadImageScans"
      Effect   = "Allow"
      Action   = ["inspector2:ListCoverage", "inspector2:ListFindings"]
      Resource = "*"
    }]
  })

  tags = var.tags
}

# The gate, checks, scanner, and image verification pods run as the image-verifier ServiceAccount
# while signature verification is on. Its only permission is the revocation check the AWS Signer
# plugin makes, so code in those pods gains nothing else from it.
resource "aws_iam_policy" "image_verifier" {
  count = local.image_governance && var.image_signature_verification ? 1 : 0

  name_prefix = "${var.name_prefix}-image-verifier-"
  description = "Lets the image-verifier pods check the revocation status of an image signature."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "CheckSignatureRevocation"
      Effect   = "Allow"
      Action   = "signer:GetRevocationStatus"
      Resource = "*"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "image_verifier" {
  for_each = local.image_governance && var.image_signature_verification ? local.tenants : {}

  role       = element(split("/", module.eks_cluster.pod_identity_role_arns["tenant_${each.key}_verifier"]), 1)
  policy_arn = aws_iam_policy.image_verifier[0].arn

  depends_on = [module.eks_cluster]
}
