locals {
  # Each key is the repository suffix inv publish-platform-images pushes to.
  images = toset([
    "platform",
    "repo-intelligence",
    "otel-collector",
    "langfuse-web",
    "langfuse-worker",
    "clickhouse",
    "tls-sidecar",
  ])
}

module "repository" {
  source   = "../ecr"
  for_each = local.images

  create_repository = var.create_container_services
  repository_name   = "${var.name_prefix}/${each.key}"
  kms_key_arn       = var.kms_key_arn
  name_prefix       = var.name_prefix
  tags              = var.tags
}
